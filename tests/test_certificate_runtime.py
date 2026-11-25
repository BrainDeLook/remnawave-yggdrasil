import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from api import Error
from certificate_runtime import CORE_PROBE, Core, Listener, core_snapshot, require_root_core, restart_and_wait


def proc_output(effective=0, filesystem=0, *, tcp=True, udp=True, foreign=True):
    lines=[f'CORE 42 {effective} {filesystem}', 'FD 42 100', 'FD 42 101', 'TABLE tcp',
           'sl local_address rem_address st tx_queue rx_queue tr tm->when retrnsmt uid timeout inode']
    if tcp:
        lines.append('0: 00000000:01BB 00000000:0000 0A 00000000:00000000 00:00000000 00000000 0 0 100')
    if foreign:
        lines.append('1: 00000000:20FB 00000000:0000 0A 00000000:00000000 00:00000000 00000000 0 0 200')
    lines.extend(['TABLE udp', 'sl local_address rem_address st tx_queue rx_queue tr tm->when retrnsmt uid timeout inode'])
    if udp:
        lines.append('2: 00000000:01BB 00000000:0000 07 00000000:00000000 00:00000000 00000000 0 0 101')
    return '\n'.join(lines)


class CoreIdentityTests(unittest.TestCase):
    def test_root_uid_and_only_owned_listeners_are_trusted(self):
        with patch('certificate_runtime.run',return_value=proc_output()) as command:
            self.assertEqual(core_snapshot(),Core(0,0,frozenset([
                Listener('tcp','00000000',443),Listener('udp','00000000',443)])))
            require_root_core()
        self.assertTrue(all(call.args[:5]==('docker','exec','remnanode','sh','-c') for call in command.call_args_list))
        self.assertNotIn('inspect',str(command.call_args_list))

    def test_nonroot_effective_or_filesystem_uid_is_rejected_without_changes(self):
        for effective,filesystem in [(1000,1000),(0,1000),(1000,0)]:
            with self.subTest(effective=effective,filesystem=filesystem):
                with patch('certificate_runtime.run',return_value=proc_output(effective,filesystem)) as command:
                    with self.assertRaisesRegex(Error,'не от root'): require_root_core()
                    self.assertEqual(command.call_count,1)

    def test_no_core_or_unknown_uid_does_not_fall_back_to_container_user(self):
        for output in ['',proc_output().replace('CORE 42 0 0','CORE 42 - -')]:
            with patch('certificate_runtime.run',return_value=output) as command:
                with self.assertRaises(Error): require_root_core()
                self.assertEqual(command.call_count,1)

    @unittest.skipUnless(os.name=='posix','The /proc shell probe is exercised in Linux CI')
    def test_process_that_exits_between_comm_and_status_is_skipped(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); process=root/'42'; process.mkdir()
            (process/'comm').write_text('rw-core\n')
            # The process's status vanished after its comm had been discovered.
            result=subprocess.run(['sh','-c',CORE_PROBE.replace('/proc',str(root))],
                                  capture_output=True,text=True,check=True)
        self.assertNotIn('CORE',result.stdout)
        self.assertEqual(result.stderr,'')

    def test_multiple_cores_and_malformed_socket_data_fail_closed(self):
        for output in [proc_output()+'\nCORE 43 0 0',proc_output().replace('00000000:01BB','invalid')]:
            with patch('certificate_runtime.run',return_value=output):
                with self.assertRaises(Error): core_snapshot()


class CertificateRestartTests(unittest.TestCase):
    def test_waits_for_owned_tcp_and_selected_udp_after_delayed_start(self):
        snapshots=iter([proc_output(),'',proc_output(udp=False),proc_output()])
        def command(*args,**kwargs):
            return 'remnanode' if args[:2]==('docker','restart') else next(snapshots)
        with patch('certificate_runtime.run',side_effect=command) as commands, \
             patch('certificate_runtime.time.monotonic',side_effect=[0,0,1,2]), \
             patch('certificate_runtime.time.sleep') as sleep:
            restart_and_wait((443,))
        self.assertEqual(sleep.call_count,2)
        restart=[c for c in commands.call_args_list if c.args[:2]==('docker','restart')]
        self.assertEqual(len(restart),1)
        self.assertEqual(restart[0].args,('docker','restart','remnanode'))

    def test_first_install_does_not_require_not_yet_created_hysteria_listener(self):
        with patch('certificate_runtime.run',side_effect=[proc_output(udp=False),'remnanode',proc_output(udp=False)]), \
             patch('certificate_runtime.time.monotonic',side_effect=[0,0]):
            restart_and_wait((443,))

    def test_transient_outgoing_udp_ports_are_not_required(self):
        with patch('certificate_runtime.run',side_effect=[proc_output(),'remnanode',proc_output(udp=False)]), \
             patch('certificate_runtime.time.monotonic',side_effect=[0,0]):
            restart_and_wait((8443,))

    def test_unrelated_host_listener_cannot_satisfy_readiness(self):
        foreign=proc_output().replace('FD 42 100','FD 42 300').replace('FD 42 101','FD 42 301')
        with patch('certificate_runtime.run',side_effect=[proc_output(),'remnanode',foreign]) as commands, \
             patch('certificate_runtime.time.monotonic',side_effect=[0,0,61]), \
             patch('certificate_runtime.time.sleep'):
            with self.assertRaisesRegex(Error,'не восстановились'): restart_and_wait((443,))
        self.assertEqual(len([c for c in commands.call_args_list if c.args[:2]==('docker','restart')]),1)

    def test_missing_listener_snapshot_prevents_restart(self):
        with patch('certificate_runtime.run',return_value=proc_output(tcp=False,udp=False)) as commands:
            with self.assertRaisesRegex(Error,'определить слушатели'): restart_and_wait()
        self.assertEqual(commands.call_count,1)

    def test_boot_restore_uses_saved_verified_listeners_when_core_is_absent(self):
        listeners=frozenset([Listener('tcp','00000000',443),Listener('udp','00000000',443)])
        with patch('certificate_runtime.run',side_effect=['','remnanode',proc_output(udp=False),proc_output()]), \
             patch('certificate_runtime.time.monotonic',side_effect=[0,0,1]), \
             patch('certificate_runtime.time.sleep') as sleep:
            restart_and_wait((443,),saved_listeners=listeners)
        sleep.assert_called_once_with(2)

    def test_missing_core_without_verified_listener_state_prevents_restart(self):
        with patch('certificate_runtime.run',return_value='') as commands:
            with self.assertRaisesRegex(Error,'сохранённые слушатели'): restart_and_wait((443,))
        self.assertEqual(commands.call_count,1)

    def test_boot_restore_also_rejects_nonroot_core_after_restart(self):
        with patch('certificate_runtime.run',side_effect=['','remnanode',proc_output(1000,1000)]) as commands, \
             patch('certificate_runtime.time.monotonic',side_effect=[0,0]):
            with self.assertRaisesRegex(Error,'не от root'):
                restart_and_wait((443,),saved_listeners=frozenset([Listener('udp','00000000',443)]))
        self.assertEqual(commands.call_count,3)

    def test_retry_with_partial_live_core_retains_verified_udp_expectations(self):
        listeners=frozenset([Listener('tcp','00000000',443),Listener('udp','00000000',443)])
        with patch('certificate_runtime.run',side_effect=[proc_output(udp=False),'remnanode',proc_output(udp=False),proc_output()]), \
             patch('certificate_runtime.time.monotonic',side_effect=[0,0,1]), \
             patch('certificate_runtime.time.sleep') as sleep:
            restart_and_wait((443,),saved_listeners=listeners)
        sleep.assert_called_once_with(2)

    def test_nonroot_core_after_restart_does_not_relax_permissions(self):
        with patch('certificate_runtime.run',side_effect=[proc_output(),'remnanode',proc_output(1000,1000)]) as commands, \
             patch('certificate_runtime.time.monotonic',side_effect=[0,0]):
            with self.assertRaisesRegex(Error,'не от root'): restart_and_wait((443,))
        self.assertFalse(any(c.args[0]=='chmod' for c in commands.call_args_list))


if __name__=='__main__': unittest.main()
