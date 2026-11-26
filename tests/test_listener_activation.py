from contextlib import ExitStack
import unittest
from unittest.mock import MagicMock, patch

from api import Error
from certificate_runtime import Core, Listener
from node import ensure_listeners


class Clock:
    def __init__(self):
        self.now=0

    def monotonic(self):
        return self.now

    def sleep(self,seconds):
        self.now+=seconds


class ListenerActivationTests(unittest.TestCase):
    def setUp(self):
        self.clock=Clock()
        self.context=ExitStack()
        self.addCleanup(self.context.close)
        self.context.enter_context(patch('node.time.monotonic',side_effect=self.clock.monotonic))
        self.sleep=self.context.enter_context(patch('node.time.sleep',side_effect=self.clock.sleep))
        self.command=self.context.enter_context(patch('node.run',return_value='remnanode'))
        self.notice=self.context.enter_context(patch('ui.say'))
        self.core=self.context.enter_context(patch('certificate_runtime.core_snapshot',return_value=None))
        self.connect=self.context.enter_context(patch('node.socket.create_connection',return_value=MagicMock()))

    def test_ready_tcp_returns_without_restart_or_delay(self):
        targets=[('GRPC','127.0.0.1',8443),('Ygg','200::1',1488)]
        self.assertIsNone(ensure_listeners(targets))
        self.assertEqual([call.args[0] for call in self.connect.call_args_list],
                         [('127.0.0.1',8443),('200::1',1488)])
        self.command.assert_not_called()
        self.sleep.assert_not_called()
        self.core.assert_not_called()

    def test_delayed_activation_before_ten_seconds_does_not_restart(self):
        def connect(*args,**kwargs):
            if self.clock.now<6:
                raise ConnectionRefusedError()
            return MagicMock()
        self.connect.side_effect=connect
        ensure_listeners([('GRPC','127.0.0.1',8443)])
        self.assertEqual(self.clock.now,6)
        self.command.assert_not_called()
        self.notice.assert_not_called()

    def test_fallback_restarts_only_local_selected_container_once(self):
        def connect(*args,**kwargs):
            if not self.command.called:
                raise ConnectionRefusedError()
            return MagicMock()
        self.connect.side_effect=connect
        ensure_listeners([('GRPC','127.0.0.1',8443)])
        self.assertEqual(self.clock.now,10)
        self.command.assert_called_once_with('docker','restart','remnanode',timeout=120)
        self.notice.assert_called_once()
        self.assertEqual(self.notice.call_args.args[1],'warning')

    def test_rechecks_previously_ready_tcp_after_restart(self):
        checked=[]
        def connect(address,**kwargs):
            checked.append((self.clock.now,address[1]))
            if (not self.command.called and address[1]==8443) or (self.command.called and self.clock.now<14 and address[1]==443):
                raise ConnectionRefusedError()
            return MagicMock()
        self.connect.side_effect=connect
        ensure_listeners([('TCP','127.0.0.1',443),('GRPC','127.0.0.1',8443)])
        self.assertEqual(self.clock.now,14)
        self.assertIn((10,443),checked)
        self.assertIn((12,443),checked)
        self.assertIn((14,443),checked)
        self.command.assert_called_once_with('docker','restart','remnanode',timeout=120)

    def test_final_error_names_missing_inbound_and_does_not_repeat_restart(self):
        self.connect.side_effect=ConnectionRefusedError()
        with self.assertRaisesRegex(Error,'Не появился слушатель GRPC'):
            ensure_listeners([('GRPC','127.0.0.1',8443)])
        self.assertEqual(self.clock.now,90)
        self.command.assert_called_once_with('docker','restart','remnanode',timeout=120)

    def test_slow_restart_leaves_at_least_sixty_seconds_for_readiness(self):
        self.command.side_effect=lambda *args,**kwargs:self.clock.sleep(80)
        self.connect.side_effect=ConnectionRefusedError()
        with self.assertRaisesRegex(Error,'Не появился слушатель Ygg'):
            ensure_listeners([('Ygg','200::1',1488)])
        self.assertEqual(self.clock.now,150)
        self.command.assert_called_once_with('docker','restart','remnanode',timeout=120)

    def test_udp_only_activation_waits_for_core_owned_listener(self):
        other=Listener('udp','00000000',9999)
        hy2=Listener('udp6','00000000000000000000000000000000',443)
        def core():
            return Core(0,0,frozenset([other,hy2] if self.command.called else [other]))
        self.core.side_effect=core
        ensure_listeners([],udp_targets=(('HY2',443),))
        self.assertEqual(self.clock.now,10)
        self.command.assert_called_once_with('docker','restart','remnanode',timeout=120)
        self.connect.assert_not_called()

    def test_ready_tcp_and_udp_do_not_restart(self):
        self.core.return_value=Core(0,0,frozenset([Listener('udp','00000000',443)]))
        ensure_listeners([('GRPC','127.0.0.1',8443)],udp_targets=(('HY2',443),))
        self.command.assert_not_called()
        self.sleep.assert_not_called()
        self.core.assert_called_once()

    def test_udp_error_names_inbound_when_core_never_appears(self):
        with self.assertRaisesRegex(Error,'Не появился слушатель HY2'):
            ensure_listeners([],udp_targets=(('HY2',443),))
        self.assertEqual(self.clock.now,90)
        self.command.assert_called_once_with('docker','restart','remnanode',timeout=120)
        self.connect.assert_not_called()

    def test_no_targets_does_not_probe_or_restart(self):
        ensure_listeners([])
        self.command.assert_not_called()
        self.core.assert_not_called()
        self.connect.assert_not_called()
        self.sleep.assert_not_called()


if __name__=='__main__':
    unittest.main()
