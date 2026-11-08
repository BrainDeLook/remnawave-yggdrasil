"""Tests for the standalone IP migration tool; never contact real servers."""
import contextlib
import hashlib
from dataclasses import replace
import importlib.util
import io
import itertools
import json
import re
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch
import urllib.error


SOURCE = Path(__file__).resolve().parents[1] / "tools" / "ip-migrate" / "migrate.py"
SPEC = importlib.util.spec_from_file_location("remna_ip_migrate", SOURCE)
m = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = m
SPEC.loader.exec_module(m)

NEW = "8.8.8.8"
OLD = "9.9.9.9"
REMOTE_IP = "8.8.4.4"
LOCAL_ID = "11111111-1111-4111-8111-111111111111"
REMOTE_ID = "22222222-2222-4222-8222-222222222222"
HOST_ID = "33333333-3333-4333-8333-333333333333"
OTHER_ID = "44444444-4444-4444-8444-444444444444"


def node(address=REMOTE_IP, *, uid=REMOTE_ID, name="Remote", port=2222,
         connected=True, connecting=False, disabled=False):
    return m.Node(uid, name, address, port, disabled, connected, connecting)


def inventory(*nodes, hosts=()):
    return m.Inventory(tuple(nodes), tuple(hosts))


def firewall(rows="", *, status="active", listening=True):
    return m.Firewall.parse({"status": "Status: " + status + "\n" + rows,
                             "listening": listening}, NEW, 2222)


class BoundaryTests(unittest.TestCase):
    def test_inventory_normalizes_uuid_and_default_management_port(self):
        parsed = m.Inventory.parse({
            "nodes": [{"uuid": REMOTE_ID.upper(), "name": "Remote", "address": REMOTE_IP,
                       "port": None, "disabled": False, "connected": True, "connecting": False}],
            "hosts": [{"uuid": HOST_ID, "name": "VPN", "address": "vpn.example.test"}],
        })
        self.assertEqual(parsed.nodes, (node(port=443),))
        self.assertEqual(parsed.hosts[0].address, "vpn.example.test")

    def test_inventory_rejects_untrusted_shapes_and_bool_ports(self):
        valid = {"uuid": REMOTE_ID, "name": "Remote", "address": REMOTE_IP,
                 "port": 2222, "disabled": False, "connected": True, "connecting": False}
        cases = [None, [], {"nodes": {}, "hosts": []},
                 {"nodes": [dict(valid, port=True)], "hosts": []},
                 {"nodes": [dict(valid, port=65536)], "hosts": []},
                 {"nodes": [dict(valid, connected=1)], "hosts": []},
                 {"nodes": [dict(valid, uuid="not-a-uuid")], "hosts": []},
                 {"nodes": [dict(valid, address="")], "hosts": []},
                 {"nodes": [valid], "hosts": [{"uuid": HOST_ID, "name": "VPN", "address": []}]}]
        for value in cases:
            with self.subTest(value=value), self.assertRaises(m.Error):
                m.Inventory.parse(value)

    def test_inventory_rejects_duplicate_node_identity(self):
        value = {"uuid": REMOTE_ID, "name": "Remote", "address": REMOTE_IP,
                 "port": 2222, "disabled": False, "connected": True, "connecting": False}
        with self.assertRaises(m.Error):
            m.Inventory.parse({"nodes": [value, dict(value)], "hosts": []})

    def test_new_ip_requires_public_unicast_ipv4_without_prefix(self):
        self.assertEqual(m.public_ipv4(" 8.8.8.8 "), NEW)
        for value in ("127.0.0.1", "10.0.0.1", "::1", "8.8.8.8/32", "224.0.0.1", "bad"):
            with self.subTest(value=value), self.assertRaises(m.Error):
                m.public_ipv4(value)

    def test_local_addresses_parse_ipv4_and_ipv6(self):
        raw = [{"addr_info": [{"family": "inet", "local": NEW},
                              {"family": "inet6", "local": "2001:4860::1"}]}]
        with patch.object(m, "run", return_value=json.dumps(raw)):
            self.assertEqual(m.local_addresses(), {NEW, "2001:4860::1"})
        with patch.object(m, "run", return_value='[{"addr_info": null}]'), self.assertRaises(m.Error):
            m.local_addresses()

    def test_subprocess_errors_never_echo_arguments_or_stderr(self):
        secret = "TEST_TOKEN_NOT_FOR_OUTPUT"
        completed = subprocess.CompletedProcess(["ssh"], 255, "", secret)
        with patch.object(m.subprocess, "run", return_value=completed), self.assertRaises(m.Error) as caught:
            m.run(["ssh", secret])
        self.assertNotIn(secret, str(caught.exception))
        with patch.object(m.subprocess, "run", side_effect=subprocess.TimeoutExpired(["ssh", secret], 1)), \
                self.assertRaises(m.Error) as caught:
            m.run(["ssh", secret])
        self.assertNotIn(secret, str(caught.exception))

    def test_database_inventory_uses_read_only_transaction(self):
        raw = {"nodes": [], "hosts": []}
        env = [{"Config": {"Env": ["POSTGRES_USER=app", "POSTGRES_DB=panel", "POSTGRES_PASSWORD=secret"]}}]
        with patch.object(m, "run", side_effect=[json.dumps(env), json.dumps(raw)]) as run:
            self.assertEqual(m.Database("database").inventory(), inventory())
        command = run.call_args.args[0]
        self.assertEqual(command[:4], ["docker", "exec", "database", "psql"])
        self.assertIn("BEGIN READ ONLY", command[-1])
        self.assertNotIn("POSTGRES_PASSWORD", command[-1])
        self.assertNotIn("UPDATE ", command[-1])


class TargetTests(unittest.TestCase):
    def test_exact_target_wins_over_dns_alias(self):
        direct = m.Target("node.example.test", 22, "root", "/key")
        alias = m.Target(REMOTE_IP, 2200, "admin", "/other-key")
        with patch.object(m, "resolve", return_value=frozenset({REMOTE_IP})) as resolve:
            self.assertEqual(m.match_target(node("node.example.test"), (alias, direct)), direct)
        resolve.assert_not_called()

    def test_overlay_exact_match_and_duplicate_connection_are_safe(self):
        first = m.Target(REMOTE_IP, 22, "root", "/key", "100.64.0.2")
        duplicate = m.Target(REMOTE_IP, 22, "root", "/key", "100.64.0.2")
        self.assertEqual(m.match_target(node("100.64.0.2"), (first, duplicate)), first)

    def test_dns_match_skips_unresolvable_saved_target(self):
        live = m.Target(REMOTE_IP, 22, "root", "/key")
        stale = m.Target("stale.example.test", 22, "root", "/key")
        answers = {"node.example.test": frozenset({REMOTE_IP}), REMOTE_IP: frozenset({REMOTE_IP}),
                   "stale.example.test": frozenset()}
        with patch.object(m, "resolve", side_effect=answers.__getitem__):
            self.assertEqual(m.match_target(node("node.example.test"), (stale, live)), live)

    def test_ambiguous_dns_access_is_not_chosen_arbitrarily(self):
        targets = (m.Target(REMOTE_IP, 22, "root", "/key"),
                   m.Target("alias.example.test", 222, "admin", "/other-key"))
        with patch.object(m, "resolve", return_value=frozenset({REMOTE_IP})), self.assertRaises(m.Error):
            m.match_target(node("node.example.test"), targets)

    def test_unrelated_targets_never_match(self):
        target = m.Target("1.1.1.1", 22, "root", "/key")
        with patch.object(m, "resolve", side_effect=lambda value: frozenset({value})):
            self.assertIsNone(m.match_target(node(), (target,)))

    def test_stale_dns_resolution_is_cached_as_empty_result(self):
        m.resolve.cache_clear()
        with patch.object(m.socket, "getaddrinfo", side_effect=socket.gaierror()) as dns:
            self.assertEqual(m.resolve("stale.example.test"), frozenset())
            self.assertEqual(m.resolve("stale.example.test"), frozenset())
        dns.assert_called_once()
        m.resolve.cache_clear()

    def test_target_file_is_parsed_without_executing_shell(self):
        with tempfile.TemporaryDirectory(dir=SOURCE.parent) as folder:
            target = Path(folder) / "test.target"
            key = str(Path(folder) / "id_ed25519")
            target.write_text(f"host={REMOTE_IP}\nport=2200\nuser=root\nkey={key}\n"
                              "label=$(touch /tmp/not-executed)\n", encoding="utf-8")
            self.assertEqual(m.Target.read(target), m.Target(REMOTE_IP, 2200, "root", key))
            target.write_text(f"host=-oProxyCommand=bad\nuser=root\nkey={key}\n", encoding="utf-8")
            with self.assertRaises(m.Error):
                m.Target.read(target)


class FirewallTests(unittest.TestCase):
    def test_remote_inspection_rejects_loopback_only_listener(self):
        for endpoint, expected in (("127.0.0.1:2222", False), ("[::1]:2222", False),
                                   ("0.0.0.0:2222", True), ("*:2222", True)):
            with self.subTest(endpoint=endpoint):
                commands = [subprocess.CompletedProcess([], 0, "Status: inactive\n"),
                            subprocess.CompletedProcess([], 0, f"LISTEN 0 4096 {endpoint} *:*\n")]
                output = io.StringIO()
                with patch.object(sys, "argv", ["remote", "inspect", NEW, "2222", "test"]), \
                     patch.object(m.shutil, "which", return_value="/usr/sbin/ufw"), \
                     patch.object(subprocess, "run", side_effect=commands) as call, \
                     contextlib.redirect_stdout(output):
                    exec(compile(m.REMOTE, "remote-inspection", "exec"), {})
                self.assertEqual(json.loads(output.getvalue())["listening"], expected)
                self.assertEqual(call.call_count, 2)
                self.assertNotIn("allow", str(call.call_args_list))

    def test_launcher_checksum_matches_downloaded_source(self):
        launcher = SOURCE.with_name("install.sh").read_text(encoding="utf-8")
        expected = re.search(r"expected='([a-f0-9]{64})'", launcher)
        self.assertIsNotNone(expected, "The launcher must pin the source checksum")
        self.assertEqual(expected.group(1), hashlib.sha256(SOURCE.read_bytes()).hexdigest())

    def test_inactive_does_not_match_active_substring(self):
        fw = firewall(status="inactive")
        self.assertFalse(fw.active)
        self.assertFalse(fw.allows)
        self.assertFalse(fw.blocked)

    def test_cidr_allow_covers_new_ip_and_preserves_rule_order(self):
        fw = firewall("2222/tcp                   ALLOW IN    8.8.8.0/24\n"
                      "2222/tcp                   DENY IN     Anywhere\n")
        self.assertTrue(fw.allows)
        self.assertFalse(fw.blocked)

    def test_deny_before_allow_is_blocking(self):
        fw = firewall("2222/tcp                   DENY IN     8.8.8.0/24\n"
                      "2222/tcp                   ALLOW IN    8.8.8.8\n")
        self.assertFalse(fw.allows)
        self.assertTrue(fw.blocked)

    def test_port_range_and_multiport_deny_are_not_bypassed(self):
        for destination in ("2000:3000/tcp", "22,2222,5555/tcp", "8.8.4.4 2222/tcp",
                            "2222/tcp on eth0"):
            with self.subTest(destination=destination):
                fw = firewall(f"{destination:30}  DENY IN     8.8.8.0/24\n"
                              "2222/tcp                        ALLOW IN    8.8.8.8\n")
                self.assertTrue(fw.blocked)
                self.assertFalse(fw.allows)

    def test_allow_for_specific_destination_or_interface_is_not_universal(self):
        for destination in ("8.8.4.4 2222/tcp", "2222/tcp on eth0"):
            with self.subTest(destination=destination):
                fw = firewall(f"{destination:30}  ALLOW IN    8.8.8.8\n")
                self.assertFalse(fw.allows)
                self.assertFalse(fw.blocked)

    def test_wrong_port_udp_ipv6_and_old_source_do_not_authorize_new_ip(self):
        fw = firewall("22/tcp                     ALLOW IN    Anywhere\n"
                      "2222/udp                   ALLOW IN    Anywhere\n"
                      "2222/tcp (v6)              ALLOW IN    Anywhere (v6)\n"
                      "2222/tcp                   ALLOW IN    9.9.9.9\n")
        self.assertTrue(fw.active)
        self.assertFalse(fw.allows)
        self.assertFalse(fw.blocked)

    def test_anywhere_rule_and_comment_are_understood(self):
        self.assertTrue(firewall("2222/tcp                   ALLOW IN    Anywhere # Node API\n").allows)
        self.assertTrue(firewall("Anywhere                   ALLOW IN    8.8.8.8 # panel\n").allows)

    def test_ufw_missing_and_invalid_remote_response(self):
        missing = m.Firewall.parse({"status": "missing", "listening": True}, NEW, 2222)
        self.assertFalse(missing.active)
        for raw in ({"status": "unexpected", "listening": True},
                    {"status": "Status: active", "listening": "yes"}, None,
                    {"error": "ufw-write-failed", "backup": "/backup"}):
            with self.subTest(raw=raw), self.assertRaises(m.Error):
                m.Firewall.parse(raw, NEW, 2222)

    def test_ssh_uses_known_hosts_and_noninteractive_existing_key(self):
        with tempfile.TemporaryDirectory(dir=SOURCE.parent) as folder:
            key, known = Path(folder) / "key", Path(folder) / "known_hosts"
            key.touch()
            known.touch()
            target = m.Target(REMOTE_IP, 2200, "root", str(key))
            response = json.dumps({"status": "Status: inactive", "listening": True})
            with patch.object(m, "run", return_value=response) as run:
                m.SSH(known).inspect(target, NEW, 2222, "test_stamp")
            args = run.call_args.args[0]
            self.assertIn("BatchMode=yes", args)
            self.assertIn("IdentitiesOnly=yes", args)
            self.assertIn("StrictHostKeyChecking=yes", args)
            self.assertIn(f"UserKnownHostsFile={known}", args)
            self.assertEqual(args[args.index("-p") + 1], "2200")
            self.assertNotIn("sshpass", args)
            self.assertIn("inspect", args[-1])


class AddressAndAPITests(unittest.TestCase):
    def test_only_exact_literal_node_and_host_addresses_change(self):
        data = inventory(node(OLD), node("vpn.example.test", uid=OTHER_ID), hosts=(
            m.Host(HOST_ID, "literal", OLD),
            m.Host(LOCAL_ID, "domain", "vpn.example.test"),
            m.Host(OTHER_ID, "substring", "prefix-" + OLD)))
        changes = m.address_patches(data, OLD, NEW)
        self.assertEqual([(p.resource, p.uuid, p.old, p.new) for p in changes],
                         [("nodes", REMOTE_ID, OLD, NEW), ("hosts", HOST_ID, OLD, NEW)])
        self.assertEqual(m.address_patches(data, "", NEW), ())
        self.assertEqual(m.address_patches(data, NEW, NEW), ())
        self.assertEqual(data.nodes[0].address, OLD)

    def test_api_patch_contains_only_uuid_and_address(self):
        token = "TEST_TOKEN_NOT_FOR_OUTPUT"
        change = m.Patch("nodes", REMOTE_ID, "Remote", OLD, NEW)
        api = m.API(token)
        response = Mock()
        expected = {"uuid": REMOTE_ID, "address": NEW}
        response.read.return_value = json.dumps({"response": expected}).encode()
        context = Mock()
        context.__enter__ = Mock(return_value=response)
        context.__exit__ = Mock(return_value=False)
        with patch.object(api.opener, "open", return_value=context) as send:
            self.assertEqual(api.request("nodes", change), expected)
        request = send.call_args.args[0]
        self.assertEqual(request.full_url, "http://127.0.0.1:3000/api/nodes")
        self.assertEqual(request.get_method(), "PATCH")
        self.assertEqual(json.loads(request.data), {"uuid": REMOTE_ID, "address": NEW})
        self.assertEqual(request.get_header("Authorization"), "Bearer " + token)
        self.assertEqual(request.get_header("X-forwarded-proto"), "https")
        self.assertEqual(request.get_header("X-forwarded-for"), "127.0.0.1")

    def test_api_auth_error_does_not_print_token_or_body(self):
        token = "TEST_TOKEN_NOT_FOR_OUTPUT"
        api = m.API(token)
        error = urllib.error.HTTPError("http://localhost", 403, token, {}, io.BytesIO(token.encode()))
        with patch.object(api.opener, "open", side_effect=error), self.assertRaises(m.Error) as caught:
            api.request("nodes")
        self.assertNotIn(token, str(caught.exception))
        self.assertIn("Nodes/Hosts", str(caught.exception))
        error.close()

    def test_successful_http_without_confirmed_new_address_is_rejected(self):
        api = m.API("TEST_TOKEN")
        context = Mock()
        context.__enter__ = Mock(return_value=Mock(read=Mock(return_value=b'{"response":{"uuid":"wrong"}}')))
        context.__exit__ = Mock(return_value=False)
        change = m.Patch("hosts", HOST_ID, "Host", OLD, NEW)
        with patch.object(api.opener, "open", return_value=context), self.assertRaises(m.Error):
            api.request("hosts", change)


class VerifyTests(unittest.TestCase):
    def test_success_requires_fresh_database_status_and_tcp(self):
        db = Mock()
        fresh = node("1.1.1.1", port=3333)
        db.inventory.return_value = inventory(fresh)
        with patch.object(m, "tcp_open", return_value=True) as tcp, \
                patch.object(m.time, "monotonic", side_effect=[0, 0, 45]), \
                patch.object(m.time, "sleep"):
            m.verify(db, (node(connected=False),), 90)
        self.assertEqual(tcp.call_count, 2)
        self.assertTrue(all(call.args == ("1.1.1.1", 3333) for call in tcp.call_args_list))

    def test_open_tcp_alone_does_not_confirm_remnawave_connection(self):
        db = Mock()
        db.inventory.return_value = inventory(node(connected=False))
        with patch.object(m, "tcp_open", return_value=True), \
                patch.object(m.time, "monotonic", side_effect=[0, 1]), self.assertRaises(m.Error):
            m.verify(db, (node(),), 1)

    def test_fresh_connected_flag_without_tcp_still_fails(self):
        db = Mock()
        db.inventory.return_value = inventory(node())
        with patch.object(m, "tcp_open", return_value=False), \
                patch.object(m.time, "monotonic", side_effect=[0, 1]), self.assertRaises(m.Error):
            m.verify(db, (node(),), 1)

    def test_waits_for_reconnect_without_restarting_panel(self):
        db = Mock()
        db.inventory.side_effect = [inventory(node(connecting=True)), inventory(node()), inventory(node())]
        with patch.object(m, "tcp_open", return_value=True), \
                patch.object(m.time, "monotonic", side_effect=[0, 0.5, 3, 48]), \
                patch.object(m.time, "sleep") as sleep:
            m.verify(db, (node(),), 90)
        self.assertEqual(db.inventory.call_count, 3)
        self.assertEqual(sleep.call_count, 2)

    def test_disappeared_node_cannot_pass_verification(self):
        db = Mock()
        db.inventory.return_value = inventory()
        with patch.object(m, "tcp_open", return_value=True) as tcp, \
                patch.object(m.time, "monotonic", side_effect=[0, 1]), self.assertRaises(m.Error):
            m.verify(db, (node(),), 1)
        tcp.assert_not_called()


class MainTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=SOURCE.parent)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.egames = self.root / "egames"
        (self.egames / "remote-exec").mkdir(parents=True)
        (self.egames / "ssh").mkdir()
        self.key = self.egames / "ssh" / "id_ed25519"
        self.key.touch()
        (self.egames / "ssh" / "known_hosts").touch()
        (self.egames / "remote-exec" / "remote.target").write_text(
            f"host={REMOTE_IP}\nport=22\nuser=root\nkey={self.key}\n", encoding="utf-8")
        self.state = self.root / "state" / "state.json"
        self.backups = self.root / "backups"
        self.local = node("172.30.0.1", uid=LOCAL_ID, name="Local")
        self.inv = inventory(self.local, node())
        self.db = Mock()
        self.db.inventory.return_value = self.inv
        self.ssh = Mock()
        self.needs_rule = firewall("2222/tcp                   ALLOW IN    9.9.9.9\n")
        self.allowed = firewall("2222/tcp                   ALLOW IN    9.9.9.9\n"
                                "2222/tcp                   ALLOW IN    8.8.8.8\n")
        self.ssh.inspect.return_value = self.allowed
        self.output = io.StringIO()
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(contextlib.redirect_stdout(self.output))
        for name, value in (("EGAMES", self.egames), ("STATE", self.state), ("BACKUPS", self.backups)):
            stack.enter_context(patch.object(m, name, value))
        stack.enter_context(patch.object(m.sys, "platform", "linux"))
        stack.enter_context(patch.object(m.os, "geteuid", return_value=0, create=True))
        stack.enter_context(patch.object(m.shutil, "which", side_effect=lambda name: "/usr/bin/" + name))
        stack.enter_context(patch.object(m, "local_addresses", return_value={NEW, "172.30.0.1"}))
        stack.enter_context(patch.object(m, "Database", return_value=self.db))
        stack.enter_context(patch.object(m, "SSH", return_value=self.ssh))
        self.api_class = stack.enter_context(patch.object(m, "API"))
        self.api_class.return_value.request.side_effect = self.fake_api_request
        self.tcp = stack.enter_context(patch.object(m, "tcp_open", return_value=True))
        stack.enter_context(patch.object(m.time, "monotonic", side_effect=itertools.cycle([0, 0, 46])))
        stack.enter_context(patch.object(m.time, "sleep"))

    def fake_api_request(self, resource, change=None):
        """An API PATCH becomes visible to the independent read-only DB boundary."""
        if change is not None:
            current = self.db.inventory.return_value
            items = getattr(current, resource)
            updated = tuple(replace(item, address=change.new) if item.uuid == change.uuid else item
                            for item in items)
            self.db.inventory.return_value = replace(current, **{resource: updated})
        return {}

    def test_plan_is_read_only_even_when_remote_firewall_needs_update(self):
        self.ssh.inspect.return_value = self.needs_rule
        with patch.object(m, "write_private") as write, patch("builtins.input") as read:
            self.assertEqual(m.main(["--new-ip", NEW, "--plan"]), 0)
        write.assert_not_called()
        read.assert_not_called()
        self.api_class.assert_not_called()
        self.assertFalse(self.state.exists())
        self.assertFalse(self.backups.exists())
        self.assertTrue(all(not call.kwargs.get("apply", False) for call in self.ssh.inspect.call_args_list))
        self.assertEqual(self.db.inventory.call_count, 1)

    def test_apply_adds_only_new_ip_tcp_management_rule_and_records_backup_plan(self):
        self.ssh.inspect.side_effect = [self.needs_rule, self.needs_rule, self.allowed]
        self.assertEqual(m.main(["--new-ip", NEW, "--old-ip", OLD, "--yes"]), 0)
        apply_calls = [call for call in self.ssh.inspect.call_args_list if call.kwargs.get("apply")]
        self.assertEqual(len(apply_calls), 1)
        self.assertEqual(apply_calls[0].args[1:3], (NEW, 2222))
        self.api_class.assert_not_called()
        plans = list(self.backups.glob("*/plan.json"))
        self.assertEqual(len(plans), 1)
        plan = json.loads(plans[0].read_text(encoding="utf-8"))
        self.assertEqual(plan["old_ip"], OLD)
        self.assertEqual(plan["patches"], [])
        self.assertEqual(plan["firewall"][0]["port"], 2222)
        self.assertEqual(json.loads(self.state.read_text(encoding="utf-8"))["panel_ip"], NEW)
        self.assertGreaterEqual(self.db.inventory.call_count, 3)

    def test_repeated_apply_does_not_write_existing_ufw_rule(self):
        for _ in range(2):
            self.assertEqual(m.main(["--new-ip", NEW, "--yes"]), 0)
        self.assertFalse(self.backups.exists())
        self.assertTrue(all(not call.kwargs.get("apply", False) for call in self.ssh.inspect.call_args_list))
        self.api_class.assert_not_called()

    def test_ip_not_assigned_aborts_before_database_or_firewall(self):
        with patch.object(m, "local_addresses", return_value={OLD}), self.assertRaises(m.Error):
            m.main(["--new-ip", NEW, "--yes"])
        self.db.inventory.assert_not_called()
        self.ssh.inspect.assert_not_called()
        self.assertFalse(self.state.exists())

    def test_refused_confirmation_does_not_write(self):
        self.ssh.inspect.return_value = self.needs_rule
        with patch("builtins.input", return_value="нет"):
            self.assertEqual(m.main(["--new-ip", NEW]), 0)
        self.assertEqual(self.ssh.inspect.call_count, 1)
        self.assertFalse(self.backups.exists())
        self.assertFalse(self.state.exists())

    def test_missing_ssh_access_aborts_before_any_remote_change(self):
        (self.egames / "remote-exec" / "remote.target").unlink()
        self.db.inventory.return_value = inventory(self.local, node(connected=False))
        with self.assertRaises(m.Error):
            m.main(["--new-ip", NEW, "--yes"])
        self.ssh.inspect.assert_not_called()
        self.assertFalse(self.backups.exists())

    def test_mixed_local_and_remote_dns_never_silently_skips_remote_firewall(self):
        self.db.inventory.return_value = inventory(self.local, node("mixed.example.test"))
        answers = {"172.30.0.1": frozenset({"172.30.0.1"}),
                   "mixed.example.test": frozenset({NEW, REMOTE_IP})}
        with patch.object(m, "resolve", side_effect=answers.__getitem__), self.assertRaises(m.Error):
            m.main(["--new-ip", NEW, "--yes"])
        self.ssh.inspect.assert_not_called()
        self.assertFalse(self.backups.exists())

    def test_several_remote_ipv4_endpoints_are_not_chosen_arbitrarily(self):
        self.db.inventory.return_value = inventory(self.local, node("multi.example.test"))
        answers = {"172.30.0.1": frozenset({"172.30.0.1"}),
                   "multi.example.test": frozenset({REMOTE_IP, "1.1.1.1"})}
        with patch.object(m, "resolve", side_effect=answers.__getitem__), self.assertRaises(m.Error):
            m.main(["--new-ip", NEW, "--yes"])
        self.ssh.inspect.assert_not_called()
        self.assertFalse(self.backups.exists())

    def test_inactive_ufw_is_not_enabled_on_already_reachable_node(self):
        self.ssh.inspect.return_value = firewall(status="inactive")
        self.assertEqual(m.main(["--new-ip", NEW, "--yes"]), 0)
        self.assertTrue(all(not call.kwargs.get("apply", False) for call in self.ssh.inspect.call_args_list))
        self.assertFalse(self.backups.exists())

    def test_literal_addresses_use_api_minimal_patches_without_touching_other_objects(self):
        data = inventory(node(OLD, uid=LOCAL_ID, name="Local"), node(), hosts=(
            m.Host(HOST_ID, "Local host", OLD), m.Host(OTHER_ID, "Domain host", "vpn.example.test")))
        self.db.inventory.return_value = data
        (self.egames / "token").write_text("TEST_TOKEN_NOT_FOR_OUTPUT", encoding="utf-8")
        with patch.object(m, "verify") as verify:
            self.assertEqual(m.main(["--new-ip", NEW, "--old-ip", OLD, "--yes"]), 0)
        calls = self.api_class.return_value.request.call_args_list
        patches = [call.args[1] for call in calls if len(call.args) == 2]
        self.assertEqual({(p.resource, p.uuid, p.new) for p in patches},
                         {("nodes", LOCAL_ID, NEW), ("hosts", HOST_ID, NEW)})
        self.assertEqual({call.args[0] for call in calls}, {"nodes", "hosts"})
        verify.assert_called_once()
        self.assertNotIn("TEST_TOKEN_NOT_FOR_OUTPUT", self.output.getvalue())

    def test_configuration_changed_since_preflight_aborts_before_writes(self):
        self.ssh.inspect.return_value = self.needs_rule
        self.db.inventory.side_effect = [self.inv, inventory(self.local, node(port=3333))]
        with self.assertRaises(m.Error):
            m.main(["--new-ip", NEW, "--yes"])
        self.assertEqual(self.ssh.inspect.call_count, 1)
        self.assertFalse(self.backups.exists())
        self.assertFalse(self.state.exists())

    def test_failed_verification_does_not_store_success_or_print_ready(self):
        self.db.inventory.return_value = inventory(self.local, node(connected=False))
        with patch.object(m.time, "monotonic", side_effect=[0, 45]), self.assertRaises(m.Error):
            m.main(["--new-ip", NEW, "--yes", "--timeout", "45"])
        self.assertFalse(self.state.exists())
        self.assertNotIn("ГОТОВО", self.output.getvalue())

    def test_disabled_nodes_are_neither_changed_nor_required_for_success(self):
        self.db.inventory.return_value = inventory(self.local, node(disabled=True, connected=False))
        self.assertEqual(m.main(["--new-ip", NEW, "--yes"]), 0)
        self.ssh.inspect.assert_not_called()
        self.api_class.assert_not_called()
        self.assertIn("Отключённые в панели ноды пропущены: 1", self.output.getvalue())

    def test_proxy_managed_node_is_not_treated_as_direct_panel_connection(self):
        remote = m.Node(REMOTE_ID, "Remote", REMOTE_IP, 2222, False, True, False,
                        "http://proxy.example.test:8080")
        self.db.inventory.return_value = inventory(self.local, remote)
        with self.assertRaises(m.Error):
            m.main(["--new-ip", NEW, "--yes"])
        self.ssh.inspect.assert_not_called()
        self.assertFalse(self.backups.exists())
        self.assertFalse(self.state.exists())

    def test_expired_saved_token_can_be_replaced_without_saving_new_token(self):
        data = inventory(node(OLD, uid=LOCAL_ID, name="Local"), node(),
                         hosts=(m.Host(HOST_ID, "Local host", OLD),))
        self.db.inventory.return_value = data
        token = self.egames / "token"
        token.write_text("EXPIRED_TEST_TOKEN", encoding="utf-8")
        old_api, new_api = Mock(), Mock()
        old_api.request.side_effect = m.AccessError("expired")
        new_api.request.side_effect = self.fake_api_request
        self.api_class.side_effect = [old_api, new_api]
        with patch.object(m.getpass, "getpass", return_value="NEW_TEST_TOKEN") as prompt, \
                patch.object(m, "verify"):
            self.assertEqual(m.main(["--new-ip", NEW, "--old-ip", OLD, "--yes"]), 0)
        prompt.assert_called_once()
        self.assertEqual(token.read_text(encoding="utf-8"), "EXPIRED_TEST_TOKEN")
        self.assertEqual({call.args[0] for call in new_api.request.call_args_list}, {"nodes", "hosts"})
        self.assertNotIn("NEW_TEST_TOKEN", self.output.getvalue())

    def test_read_only_token_can_be_replaced_at_patch_boundary(self):
        self.db.inventory.return_value = inventory(self.local, node(), hosts=(m.Host(HOST_ID, "Host", OLD),))
        token = self.egames / "token"
        token.write_text("READ_ONLY_TEST_TOKEN", encoding="utf-8")
        old_api, new_api = Mock(), Mock()
        old_api.request.side_effect = [{}, m.AccessError("write forbidden")]
        new_api.request.side_effect = self.fake_api_request
        self.api_class.side_effect = [old_api, new_api]
        with patch.object(m.getpass, "getpass", return_value="WRITABLE_TEST_TOKEN") as prompt, \
                patch.object(m, "verify"):
            self.assertEqual(m.main(["--new-ip", NEW, "--old-ip", OLD, "--yes"]), 0)
        prompt.assert_called_once()
        self.assertEqual(new_api.request.call_args.args[0], "hosts")
        self.assertEqual(new_api.request.call_args.args[1].new, NEW)
        self.assertEqual(token.read_text(encoding="utf-8"), "READ_ONLY_TEST_TOKEN")
        self.assertNotIn("WRITABLE_TEST_TOKEN", self.output.getvalue())


if __name__ == "__main__":
    unittest.main()
