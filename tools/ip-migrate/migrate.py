#!/usr/bin/env python3
"""Change a panel's management IP without changing VPN configuration. Stdlib only."""
from __future__ import annotations

import argparse
import getpass
import ipaddress
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import lru_cache
from uuid import UUID, uuid4

VERSION = "1.0.0"
EGAMES = Path("/usr/local/remnawave_reverse")
STATE = Path("/var/lib/remnawave-ip-migrate/state.json")
BACKUPS = Path("/var/backups/remnawave-ip-migrate")


class Error(RuntimeError):
    pass


class AccessError(Error):
    pass


def public_ipv4(value: str) -> str:
    try:
        address = ipaddress.IPv4Address(value.strip())
    except ipaddress.AddressValueError as exc:
        raise Error("Нужен IPv4 без маски, например 8.8.8.8.") from exc
    if not address.is_global or address.is_multicast:
        raise Error("Нужен внешний IPv4 VPS; локальный адрес сюда не подходит.")
    return str(address)


def run(args: list[str], *, timeout: int = 25) -> str:
    """Never expose command arguments or raw stderr: they may contain credentials."""
    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=timeout,
                                encoding="utf-8", errors="replace")
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise Error(f"Не удалось выполнить {Path(args[0]).name}: ошибка запуска или тайм-аут.") from exc
    if result.returncode:
        raise Error(f"{Path(args[0]).name}: команда завершилась с кодом {result.returncode}.")
    return result.stdout


def read_json(value: str, source: str):
    try:
        return json.loads(value)
    except (ValueError, TypeError) as exc:
        raise Error(f"Некорректный JSON: {source}.") from exc


@dataclass(frozen=True)
class Node:
    uuid: str
    name: str
    address: str
    port: int
    disabled: bool
    connected: bool
    connecting: bool
    proxy: str | None = None


@dataclass(frozen=True)
class Host:
    uuid: str
    name: str
    address: str


@dataclass(frozen=True)
class Inventory:
    nodes: tuple[Node, ...]
    hosts: tuple[Host, ...]

    @classmethod
    def parse(cls, raw) -> Inventory:
        """The database JSON is checked once, at this boundary."""
        try:
            nodes, hosts = raw["nodes"], raw["hosts"]
            if not isinstance(nodes, list) or not isinstance(hosts, list):
                raise ValueError("lists")
            parsed_nodes = []
            for item in nodes:
                uid = str(UUID(item["uuid"]))
                name, address = item["name"], item["address"]
                # Backend uses https://address when port is null, hence TCP 443.
                port = item["port"] if item["port"] is not None else 443
                flags = [item[k] for k in ("disabled", "connected", "connecting")]
                proxy = item.get("proxy")
                if (not isinstance(name, str) or not isinstance(address, str) or not address
                        or type(port) is not int or not 1 <= port <= 65535
                        or any(type(flag) is not bool for flag in flags)
                        or (proxy is not None and not isinstance(proxy, str))):
                    raise ValueError("node fields")
                parsed_nodes.append(Node(uid, name, address, port, *flags, proxy))
            parsed_hosts = []
            for item in hosts:
                uid = str(UUID(item["uuid"]))
                if not isinstance(item["name"], str) or not isinstance(item["address"], str):
                    raise ValueError("host fields")
                parsed_hosts.append(Host(uid, item["name"], item["address"]))
        except (KeyError, TypeError, ValueError, AttributeError) as exc:
            raise Error("Неожиданный формат БД. Нужна установленная Remnawave 3.x.") from exc
        if len({n.uuid for n in parsed_nodes}) != len(parsed_nodes):
            raise Error("БД вернула повторяющиеся UUID нод.")
        return cls(tuple(parsed_nodes), tuple(parsed_hosts))


SQL = """BEGIN READ ONLY;
SELECT json_build_object(
 'nodes', COALESCE((SELECT json_agg(json_build_object(
   'uuid',uuid,'name',name,'address',address,'port',port,
   'disabled',is_disabled,'connected',is_connected,'connecting',is_connecting,'proxy',proxy_url)
   ORDER BY view_position) FROM nodes),'[]'::json),
 'hosts', COALESCE((SELECT json_agg(json_build_object(
   'uuid',uuid,'name',remark,'address',address)) FROM hosts),'[]'::json));
COMMIT;"""


class Database:
    def __init__(self, container: str):
        raw = read_json(run(["docker", "inspect", container]), "docker inspect БД")
        try:
            env = dict(v.split("=", 1) for v in raw[0]["Config"]["Env"] if "=" in v)
            self.user = env.get("POSTGRES_USER", "postgres")
            self.name = env.get("POSTGRES_DB", self.user)
            if not self.user or not self.name:
                raise ValueError("empty database")
        except (IndexError, KeyError, TypeError, ValueError) as exc:
            raise Error("Не удалось определить локальную БД Remnawave.") from exc
        self.container = container

    def inventory(self) -> Inventory:
        output = run(["docker", "exec", self.container, "psql", "-X", "-qAt",
                      "-v", "ON_ERROR_STOP=1", "-U", self.user, "-d", self.name,
                      "-c", SQL])
        return Inventory.parse(read_json(output, "список нод и Host"))


def local_addresses() -> set[str]:
    raw = read_json(run(["ip", "-j", "address", "show"]), "локальные адреса VPS")
    try:
        return {str(ipaddress.ip_address(a["local"])) for interface in raw
                for a in interface["addr_info"] if a["family"] in ("inet", "inet6")}
    except (KeyError, TypeError, ValueError) as exc:
        raise Error("Не удалось прочитать IP-адреса VPS.") from exc


@lru_cache(maxsize=None)
def resolve(address: str) -> frozenset[str]:
    try:
        return frozenset({str(ipaddress.ip_address(address))})
    except ValueError:
        pass
    try:
        return frozenset(info[4][0] for info in socket.getaddrinfo(
            address, None, family=socket.AF_INET, type=socket.SOCK_STREAM))
    except socket.gaierror:
        return frozenset()


@dataclass(frozen=True)
class Target:
    host: str
    port: int
    user: str
    key: str
    overlay: str = ""

    @classmethod
    def read(cls, path: Path) -> Target:
        values = {}
        for line in path.read_text(encoding="utf-8").splitlines():
            key, sep, value = line.partition("=")
            if sep:
                values[key.strip()] = value.strip()
        try:
            host, user, key = values["host"], values["user"], values["key"]
            port = int(values.get("port", "22"))
            overlay = values.get("overlay", "")
            if (not re.fullmatch(r"[A-Za-z0-9_.:\-]+", host) or host.startswith("-")
                    or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.-]*", user)
                    or not Path(key).is_absolute() or not 1 <= port <= 65535
                    or (overlay and not re.fullmatch(r"[A-Za-z0-9_.:\-]+", overlay))):
                raise ValueError("invalid target")
        except (KeyError, ValueError) as exc:
            raise Error(f"Некорректный SSH target eGames: {path.name}.") from exc
        return cls(host, port, user, key, overlay)


def match_target(node: Node, targets: tuple[Target, ...]) -> Target | None:
    exact = {t for t in targets if node.address in (t.host, t.overlay)}
    matches = exact or {t for t in targets if resolve(node.address) & resolve(t.host)}
    # Ignore labels/overlay when two files describe the same SSH connection.
    unique = {(t.host, t.port, t.user, t.key): t for t in matches}
    if len(unique) > 1:
        raise Error(f"Несколько разных SSH-доступов для ноды «{clean(node.name)}». Уточните target eGames.")
    return next(iter(unique.values()), None)


def tcp_open(address: str, port: int) -> bool:
    try:
        with socket.create_connection((address, port), timeout=4):
            return True
    except OSError:
        return False


# Constant program, arguments are quoted separately; no remote shell expansion of user data.
REMOTE = r'''
import datetime, ipaddress, json, os, pathlib, re, shutil, subprocess, sys
mode, address, port, stamp = sys.argv[1:]
address = str(ipaddress.IPv4Address(address))
port = int(port)
if not 1 <= port <= 65535 or not re.fullmatch(r"[A-Za-z0-9_-]+", stamp):
    sys.exit(2)
env = dict(os.environ, LC_ALL="C")
ufw = shutil.which("ufw")
backup = None
if mode == "apply":
    if not ufw:
        print(json.dumps({"error":"ufw-missing"})); sys.exit(0)
    status = subprocess.run([ufw,"status"],env=env,capture_output=True,text=True,check=True).stdout
    if status.splitlines()[0].strip() != "Status: active":
        print(json.dumps({"error":"ufw-not-active"})); sys.exit(0)
    root = pathlib.Path("/var/backups/remnawave-ip-migrate")
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    backup = root / stamp
    backup.mkdir(mode=0o700)
    for name in ("user.rules","user6.rules"):
        path = pathlib.Path("/etc/ufw") / name
        if path.exists():
            shutil.copy2(path, backup / name); (backup / name).chmod(0o600)
    (backup / "status.txt").write_text(status); (backup / "status.txt").chmod(0o600)
    changed = subprocess.run([ufw,"allow","from",address,"to","any","port",str(port),
                              "proto","tcp","comment","Remnawave panel IP"],
                             env=env,capture_output=True,text=True)
    if changed.returncode:
        print(json.dumps({"error":"ufw-write-failed","backup":str(backup)})); sys.exit(0)
status = subprocess.run([ufw,"status"],env=env,capture_output=True,text=True,check=True).stdout if ufw else "missing"
listeners = subprocess.run(["ss","-lntH"],capture_output=True,text=True,check=True).stdout
listening = False
for line in listeners.splitlines():
    fields = line.split()
    if len(fields) < 4 or not fields[3].endswith(":"+str(port)):
        continue
    host = fields[3].rsplit(":",1)[0].strip("[]")
    if host not in ("*","0.0.0.0","::") and ipaddress.ip_address(host.split("%",1)[0]).is_loopback:
        continue
    listening = True
print(json.dumps({"status":status,"listening":listening,"backup":str(backup) if backup else None}))
'''


@dataclass(frozen=True)
class Firewall:
    active: bool
    allows: bool
    blocked: bool
    listening: bool
    label: str

    @classmethod
    def parse(cls, raw, address: str, port: int) -> Firewall:
        try:
            if raw.get("error"):
                raise Error("Нода не смогла применить UFW; резервная копия, если создана: "
                            + str(raw.get("backup") or "нет"))
            status, listening = raw["status"], raw["listening"]
            if not isinstance(status, str) or type(listening) is not bool:
                raise ValueError("fields")
        except (KeyError, TypeError, AttributeError, ValueError) as exc:
            raise Error("Неожиданный ответ SSH-проверки firewall.") from exc
        first = status.splitlines()[0].strip() if status else ""
        if first in ("Status: inactive", "missing"):
            return cls(False, False, False, listening,
                       "UFW выключен" if first == "Status: inactive" else "UFW отсутствует")
        if first != "Status: active":
            raise Error("Не удалось определить состояние UFW на ноде.")
        ip = ipaddress.IPv4Address(address)
        for line in status.splitlines()[1:]:
            # Unnumbered UFW status preserves rule order; an earlier DENY wins.
            row = re.match(r"^(.+?)\s{2,}(ALLOW|DENY|REJECT)(?: IN)?\s+(.+?)\s*$", line)
            if not row:
                continue
            destination, action, source = row.groups()
            source = source.split("#", 1)[0].strip()
            if "(v6)" in destination or "(v6)" in source:
                continue
            destination = destination.strip()
            if source == "Anywhere":
                matches = True
            else:
                try:
                    matches = ip in ipaddress.IPv4Network(source, strict=False)
                except ValueError:
                    continue
            if not matches:
                continue
            qualified = " " in destination
            # Parse UFW's port lists/ranges too. A destination/interface-specific DENY
            # may apply; do not append an ineffective ALLOW below it.
            fields = destination.split()
            if "on" in fields:
                fields = fields[:fields.index("on")]
            ports = fields[-1] if fields else ""
            specification, _, protocol = ports.partition("/")
            if protocol and protocol != "tcp":
                continue
            covers = specification == "Anywhere"
            if re.fullmatch(r"\d+(?::\d+)?(?:,\d+(?::\d+)?)*", specification):
                for segment in specification.split(","):
                    start, _, end = segment.partition(":")
                    covers |= int(start) <= port <= int(end or start)
            elif specification != "Anywhere":
                # Service names cannot be safely resolved without application profiles.
                if action != "ALLOW":
                    raise Error("UFW содержит запрет с неизвестным сервисом/форматом. Проверьте правила вручную.")
                continue
            if covers and (action != "ALLOW" or not qualified):
                return cls(True, action == "ALLOW", action != "ALLOW", listening,
                           "новый IP уже разрешён" if action == "ALLOW" else "перед разрешением стоит запрет")
        return cls(True, False, False, listening, "нужно разрешить новый IP")


class SSH:
    def __init__(self, known_hosts: Path):
        self.known_hosts = known_hosts

    def inspect(self, target: Target, address: str, port: int, stamp: str,
                apply: bool = False) -> Firewall:
        if not Path(target.key).is_file() or not self.known_hosts.is_file():
            raise Error("Нет сохранённого SSH-ключа или known_hosts eGames. Настройте SSH-доступ в eGames.")
        command = shlex.join(["python3", "-c", REMOTE, "apply" if apply else "inspect",
                              address, str(port), f"{stamp}_{port}"])
        if target.user != "root":
            command = "sudo -n " + command
        try:
            output = run(["ssh", "-n", "-i", target.key, "-p", str(target.port),
                          "-o", "BatchMode=yes", "-o", "IdentitiesOnly=yes",
                          "-o", "StrictHostKeyChecking=yes", "-o", "UpdateHostKeys=no", "-o", "ConnectTimeout=8",
                          "-o", "ConnectionAttempts=1", "-o", "LogLevel=ERROR",
                          "-o", f"UserKnownHostsFile={self.known_hosts}",
                          f"{target.user}@{target.host}", command], timeout=35)
        except Error as exc:
            raise Error(f"Нет SSH-доступа к {target.host}:{target.port}. Проверьте ключ, known_hosts "
                        "и доступность сервера в меню eGames; пароль скрипт не подбирает.") from exc
        return Firewall.parse(read_json(output, "SSH firewall"), address, port)


@dataclass(frozen=True)
class Check:
    node: Node
    target: Target | None
    firewall: Firewall | None
    local: bool


@dataclass(frozen=True)
class Patch:
    resource: str
    uuid: str
    name: str
    old: str
    new: str


def address_patches(inventory: Inventory, old: str, new: str) -> tuple[Patch, ...]:
    if not old or old == new:
        return ()
    return tuple(Patch(resource, item.uuid, item.name, old, new)
                 for resource, items in (("nodes", inventory.nodes), ("hosts", inventory.hosts))
                 for item in items if item.address == old)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class API:
    def __init__(self, token: str):
        if not token or any(char in token for char in "\r\n"):
            raise Error("Пустой или некорректный API-токен.")
        self.token = token
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())

    def request(self, resource: str, patch: Patch | None = None):
        body = json.dumps({"uuid": patch.uuid, "address": patch.new}).encode() if patch else None
        request = urllib.request.Request("http://127.0.0.1:3000/api/" + resource, data=body,
            method="PATCH" if patch else "GET", headers={
                "Authorization": "Bearer " + self.token, "Content-Type": "application/json",
                "X-Forwarded-For": "127.0.0.1", "X-Forwarded-Proto": "https",
                "X-Remnawave-Client-Type": "browser"})
        try:
            with self.opener.open(request, timeout=15) as response:
                value = read_json(response.read().decode(), "локальный API")
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403):
                raise AccessError("API-токен истёк или не имеет прав Nodes/Hosts. Нужен токен с чтением и изменением.") from exc
            raise Error(f"Локальный API вернул HTTP {exc.code}; ответ не выводится.") from exc
        except (OSError, UnicodeError) as exc:
            raise Error("Локальный API 127.0.0.1:3000 недоступен.") from exc
        if not isinstance(value, dict) or "response" not in value:
            raise Error("Неожиданный формат ответа локального API.")
        if patch:
            item = value["response"]
            if not isinstance(item, dict) or item.get("uuid") != patch.uuid or item.get("address") != patch.new:
                raise Error("API не подтвердил обновление адреса. Проверьте запись в панели перед повторным запуском.")
        return value["response"]


def clean(text: str) -> str:
    return "".join(char for char in text if char.isprintable())[:120]


def say(text: str, color: str = ""):
    colors = {"title": "38;2;60;205;255", "ok": "38;2;90;220;140",
              "warn": "38;2;255;205;90", "error": "38;2;255;100;110", "dim": "38;2;155;170;185"}
    if sys.stdout.isatty() and color and "NO_COLOR" not in os.environ:
        print(f"\033[{colors[color]}m{text}\033[0m")
    else:
        print(text)


def write_private(path: Path, value):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with open(path, "x", encoding="utf-8", opener=lambda p, f: os.open(p, f, 0o600)) as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write("\n")


def verify(db: Database, nodes: tuple[Node, ...], timeout: int):
    deadline = time.monotonic() + timeout
    healthy_since = None
    while True:
        fresh = {n.uuid: n for n in db.inventory().nodes}
        pending = [n for n in nodes if n.uuid not in fresh or not fresh[n.uuid].connected
                   or fresh[n.uuid].connecting
                   or not tcp_open(fresh[n.uuid].address, fresh[n.uuid].port)]
        now = time.monotonic()
        if pending:
            healthy_since = None
        elif healthy_since is None:
            healthy_since = now
        elif now - healthy_since >= 45:
            # Remnawave polls every 10s, with up to two 15s requests. Allow those
            # checks to finish rather than trusting a pre-migration connected flag.
            return
        if now >= deadline:
            raise Error("Не удалось подтвердить устойчивую связь с нодами: "
                        + ", ".join(clean(n.name) for n in (pending or nodes))
                        + ". Правила сохранены; проверьте диагностику этих нод в панели.")
        time.sleep(3)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Смена IP VPS панели Remnawave/eGames; DNS меняете вы.")
    parser.add_argument("--new-ip", help="новый внешний IPv4, уже назначенный этой VPS")
    parser.add_argument("--old-ip", help="прежний IPv4 для точечной замены literal-адресов Nodes/Hosts")
    parser.add_argument("--plan", "--dry-run", action="store_true", help="только проверка и план, без записей")
    parser.add_argument("--yes", action="store_true", help="применить показанный план без подтверждения")
    parser.add_argument("--timeout", type=int, default=90, help="ожидание восстановления нод, секунд (90)")
    parser.add_argument("--db-container", default="remnawave-db", help="контейнер PostgreSQL Remnawave")
    args = parser.parse_args(argv)
    if sys.platform != "linux" or os.geteuid() != 0:
        raise Error("Запустите от root на Linux VPS самой панели Remnawave.")
    if not 45 <= args.timeout <= 900:
        raise Error("--timeout должен быть от 45 до 900 секунд.")
    for command in ("docker", "ip", "ssh"):
        if not shutil.which(command):
            raise Error(f"Не найден {command}. Нужна действующая установка панели eGames с SSH-клиентом.")
    say(f"\n  Remna IP · {VERSION}", "title")
    say("  Смена адреса VPS панели · настройки VPN сохраняются\n", "dim")
    new = public_ipv4(args.new_ip or input("Новый внешний IPv4 этой VPS: "))
    addresses = local_addresses()
    if new not in addresses:
        raise Error(f"{new} не назначен этой VPS. Сначала смените IP у хостинга, затем запустите скрипт на VPS панели.")
    old = public_ipv4(args.old_ip) if args.old_ip else ""
    if not old and STATE.exists():
        state = read_json(STATE.read_text(encoding="utf-8"), "предыдущий перенос")
        if not isinstance(state, dict) or not isinstance(state.get("panel_ip"), str):
            raise Error("Некорректный файл предыдущего переноса.")
        old = public_ipv4(state["panel_ip"])
    db = Database(args.db_container)
    inventory = db.inventory()
    active = tuple(n for n in inventory.nodes if not n.disabled)
    if not active:
        raise Error("В панели нет включённых нод. Ничего не изменено.")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ_") + uuid4().hex[:8]
    targets = tuple(Target.read(p) for p in sorted((EGAMES / "remote-exec").glob("*.target")))
    ssh = SSH(EGAMES / "ssh" / "known_hosts")
    checks = []
    say("[1/3] Проверяю ноды и SSH-доступ eGames…", "title")
    for node in active:
        if node.proxy:
            raise Error(f"«{clean(node.name)}»: управление настроено через дополнительный прокси. "
                        "Его исходящий IP нужно учитывать отдельно; этот режим автоматизации не поддерживается.")
        endpoints = resolve(node.address)
        if endpoints & addresses and not endpoints.issubset(addresses):
            raise Error(f"«{clean(node.name)}»: DNS одновременно указывает на эту VPS и другой сервер. "
                        "Уточните адрес ноды в панели.")
        local = node.address == old or bool(endpoints and endpoints.issubset(addresses))
        if local:
            checks.append(Check(node, None, None, True))
            say(f"  • {clean(node.name)} — локальная нода; firewall не меняется", "dim")
            continue
        if len(endpoints) > 1:
            raise Error(f"«{clean(node.name)}»: у адреса несколько IPv4. Скрипт рассчитан на один сервер "
                        "на адрес управления нодой; уточните запись в панели/DNS.")
        target = match_target(node, targets)
        if target is None:
            if node.connected and not node.connecting and tcp_open(node.address, node.port):
                checks.append(Check(node, None, None, False))
                say(f"  ✓ {clean(node.name)} — уже доступна; сохранённого SSH нет", "ok")
                continue
            raise Error(f"«{clean(node.name)}»: нет связи и сохранённого SSH-доступа eGames. "
                        "Добавьте SSH-доступ к этой ноде в eGames и повторите. Ничего не изменено.")
        fw = ssh.inspect(target, new, node.port, stamp)
        if not fw.listening:
            raise Error(f"«{clean(node.name)}»: TCP {node.port} не слушается. Сначала запустите Remnanode.")
        if fw.blocked:
            raise Error(f"«{clean(node.name)}»: UFW запрещает новый IP раньше разрешающих правил. "
                        "Нужно проверить порядок правил на этой ноде; автоматический сброс не выполняется.")
        if not fw.active and not tcp_open(node.address, node.port):
            raise Error(f"«{clean(node.name)}»: {fw.label}, но TCP {node.port} недоступен. "
                        "Проверьте сетевой firewall хостинга/другой firewall; UFW включаться не будет.")
        checks.append(Check(node, target, fw, False))
        say(f"  {'+' if fw.active and not fw.allows else '✓'} {clean(node.name)} · TCP {node.port} · {fw.label}",
            "warn" if fw.active and not fw.allows else "ok")
    patches = address_patches(inventory, old, new)
    changes = [c for c in checks if c.firewall and c.firewall.active and not c.firewall.allows]
    say("\n[2/3] План переноса", "title")
    say(f"  Новый IP панели: {new}")
    say(f"  Разрешить доступ в UFW: {len(changes)} нод. Старые правила сохраняются.")
    for patch in patches:
        say(f"  {patch.resource}: {clean(patch.name)} · {patch.old} → {patch.new}", "warn")
    if not patches:
        say("  Адреса Nodes/Hosts в панели: без изменений.", "dim")
    if not old:
        say("  Прежний IP неизвестен. Если он был прописан напрямую в Nodes/Hosts,\n"
            "  перезапустите с --old-ip ПРЕЖНИЙ_IP; доменные адреса замены не требуют.", "warn")
    say("  DNS, сеть ОС, профили, пользователи и сквады скрипт не редактирует.", "dim")
    if args.plan:
        say("\nТолько проверка. Изменений и резервных копий не создавал.", "ok")
        return 0
    api = None
    if patches:
        token_path = EGAMES / "token"
        token = token_path.read_text(encoding="utf-8").strip() if token_path.exists() else ""
        if not token:
            token = getpass.getpass("API-токен с правами чтения/изменения Nodes и Hosts (скрыто): ")
        api = API(token)
        resources = sorted({p.resource for p in patches})
        try:
            for resource in resources:
                api.request(resource)
        except AccessError:
            say("  Сохранённый токен не подходит. Нужен API-токен с правами Nodes/Hosts.", "warn")
            api = API(getpass.getpass("Новый API-токен (скрытый ввод, не сохраняется): "))
            for resource in resources:
                api.request(resource)
    if not args.yes and input("\nПрименить план? да/нет [нет]: ").strip().lower() not in ("да", "yes", "y"):
        say("Отменено. Ничего не изменено.", "dim")
        return 0
    # Check configuration identity immediately before the first mutation.
    fresh = db.inventory()
    if ([(n.uuid, n.address, n.port, n.disabled) for n in inventory.nodes]
            != [(n.uuid, n.address, n.port, n.disabled) for n in fresh.nodes]
            or inventory.hosts != fresh.hosts):
        raise Error("Состав или настройки нод/Host изменились во время проверки. Повторите запуск для нового плана.")
    say("\n[3/3] Применяю и проверяю связь с Remnawave (не меньше 45 секунд)…", "title")
    backup = BACKUPS / stamp
    journal = {"version": VERSION, "new_ip": new, "old_ip": old,
               "patches": [vars(p) for p in patches],
               "firewall": [{"node": c.node.uuid, "name": c.node.name,
                             "host": c.target.host, "port": c.node.port} for c in changes]}
    if changes or patches:
        write_private(backup / "plan.json", journal)
        say(f"  План и прежние адреса: {backup}", "dim")
    for check in changes:
        # Re-read UFW to avoid acting on a changed firewall snapshot.
        fw = ssh.inspect(check.target, new, check.node.port, stamp)
        if fw.blocked or not fw.active or not fw.listening:
            raise Error(f"«{clean(check.node.name)}»: состояние firewall/слушателя изменилось. Повторите проверку.")
        if not fw.allows:
            fw = ssh.inspect(check.target, new, check.node.port, stamp, apply=True)
            if not fw.allows:
                raise Error(f"«{clean(check.node.name)}»: UFW не подтвердил новое разрешение.")
            say(f"  ✓ {clean(check.node.name)}: разрешён {new} → TCP {check.node.port}", "ok")
            say(f"    Снимок UFW на ноде: /var/backups/remnawave-ip-migrate/{stamp}_{check.node.port}", "dim")
    for patch in patches:
        current = db.inventory()
        items = current.nodes if patch.resource == "nodes" else current.hosts
        if not any(item.uuid == patch.uuid and item.address == patch.old for item in items):
            raise Error(f"«{clean(patch.name)}»: адрес уже изменён другим процессом. Повторите проверку.")
        try:
            api.request(patch.resource, patch)
        except AccessError:
            say("  Токен разрешает чтение, но не изменение. Нужны права изменения Nodes/Hosts.", "warn")
            api = API(getpass.getpass("Новый API-токен (скрытый ввод, не сохраняется): "))
            current = db.inventory()
            items = current.nodes if patch.resource == "nodes" else current.hosts
            if not any(item.uuid == patch.uuid and item.address == patch.old for item in items):
                raise Error(f"«{clean(patch.name)}»: адрес изменён во время ввода токена. Повторите проверку.")
            api.request(patch.resource, patch)
        say(f"  ✓ {patch.resource}: {clean(patch.name)} — адрес обновлён", "ok")
    verify(db, active, args.timeout)
    final = db.inventory()
    for patch in patches:
        items = final.nodes if patch.resource == "nodes" else final.hosts
        if not any(item.uuid == patch.uuid and item.address == patch.new for item in items):
            raise Error(f"«{clean(patch.name)}»: итоговый адрес в БД не совпадает с планом. Проверьте запись в панели.")
    STATE.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = STATE.with_name(f"state-{stamp}.tmp")
    write_private(temporary, {"panel_ip": new, "completed_at": stamp})
    temporary.replace(STATE)
    say(f"\n  ГОТОВО · {len(active)} из {len(active)} включённых нод подключены к Remnawave.", "ok")
    disabled = len(inventory.nodes) - len(active)
    if disabled:
        say(f"  Отключённые в панели ноды пропущены: {disabled}.", "dim")
    say("  Проверьте свои DNS-записи: их меняете вы. Перезапуск всей панели не потребовался.\n", "dim")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (Error, OSError) as exc:
        say(f"\nОшибка: {exc}", "error")
        say("Если применение уже началось, выполненные шаги сохранены. Повторный запуск безопасно продолжит перенос.", "warn")
        sys.exit(1)
    except (KeyboardInterrupt, EOFError):
        say("\nВвод отменён. Если применение уже началось, выполненные шаги сохранены.", "warn")
        sys.exit(130)
