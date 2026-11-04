import contextlib
import copy
import io
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

if os.name=='nt':
    # Result rendering does not use the Linux-only terminal input API.
    with patch.dict(sys.modules,{'termios':SimpleNamespace()}):
        import multi
else:
    import multi

class ResultTests(unittest.TestCase):
    def setUp(self):
        self.directory=tempfile.TemporaryDirectory(dir=Path.cwd())
        self.addCleanup(self.directory.cleanup)
        self.root=Path(self.directory.name)
        self.state=dict(panel='https://panel.example.com',name='Нидерланды',
            profile='12345678-1234-1234-1234-123456789012',profile_name='Нидерланды · профиль',
            address='200:1111:2222:3333:4444:5555:6666:7777',access_mode='restricted',
            allowed=['200:aaaa:bbbb:cccc:dddd:eeee:ffff:1111','300:aaaa:bbbb:cccc::/64'],
            ssh=True,ssh_port=2223,ssh_allowed=['300:aaaa:bbbb:cccc::/64'],
            saved_at='2026-11-04T12:00:00+00:00',verification='listeners',
            checked_at='2026-11-04T12:01:00+00:00',checked_protocols=['ygg'],
            hosts=[dict(protocol='ygg',name='Нидерланды Ygg',tag='NODE-YGG-1488',
                        address='200:1111:2222:3333:4444:5555:6666:7777',port=1488,disabled=False)])

    def render(self,state=None,after_install=False):
        (self.root/'multi-state.json').write_text(json.dumps(state or self.state),encoding='utf-8')
        # Old personal exports must never be mistaken for a current common subscription.
        (self.root/'client.txt').write_text('vless://OLD_PRIVATE_USER_LINK',encoding='utf-8')
        output=io.StringIO()
        with patch.object(multi.local,'BASE',self.root),patch.object(multi.ui,'screen',None),contextlib.redirect_stdout(output):
            multi.result(after_install=after_install)
        return output.getvalue()

    def test_saved_result_is_a_snapshot_not_a_new_success(self):
        output=self.render()
        self.assertNotIn('УСТАНОВКА ЗАВЕРШЕНА',output)
        self.assertIn('https://panel.example.com',output)
        self.assertIn('Нидерланды · профиль',output)
        self.assertIn('Последующие изменения в панели',output)
        self.assertIn('2026-11-04T12:00:00+00:00',output)
        self.assertIn('УСТАНОВКА ЗАВЕРШЕНА',self.render(after_install=True))

    def test_failed_listener_check_does_not_become_success_when_reopened(self):
        self.state['verification']='pending'
        self.state.pop('checked_at')
        self.state.pop('checked_protocols')
        output=self.render()
        self.assertIn('не завершена',output)
        self.assertNotIn('TCP-слушатели проверены:',output)
        self.assertNotIn('УСТАНОВКА ЗАВЕРШЕНА',output)

    def test_legacy_snapshot_does_not_claim_unrecorded_verification(self):
        for key in ('verification','checked_at','checked_protocols','profile_name','saved_at'):
            self.state.pop(key)
        output=self.render()
        self.assertIn('в этом снимке не сохранён',output)
        self.assertIn(self.state['profile'],output)
        self.assertNotIn('TCP-слушатели проверены:',output)

    def test_hysteria_udp_is_separate_from_tcp_and_end_to_end_verification(self):
        hy2=copy.deepcopy(self.state['hosts'][0])
        hy2.update(protocol='hy2',name='Нидерланды Histeria',tag='NODE-HY2-443',port=443)
        self.state['hosts']=[hy2]
        self.state['checked_protocols']=[]
        output=self.render()
        self.assertIn('Hysteria2 использует UDP',output)
        self.assertIn('TCP-проверки не выполнялись',output)
        self.assertNotIn('TCP-слушатели проверены:',output)
        self.assertIn('HTTPS и VPN-авторизацию',output)
        self.assertIn('проверьте в своём клиенте',output)

    def test_subscription_has_no_shared_credentials_and_does_not_reuse_old_export(self):
        output=self.render()
        self.assertIn('личную подписку VPN-пользователя',output)
        self.assertIn('Общей VPN-ссылки и UUID нет',output)
        self.assertNotIn('OLD_PRIVATE_USER_LINK',output)
        self.assertIn('[200:1111:2222:3333:4444:5555:6666:7777]:1488',output)
        self.assertIn('bind_interface: "yggdrasil"',output)

    def test_ssh_access_is_explicit_and_disabled_hosts_are_not_shown_as_available(self):
        self.state['hosts'][0]['disabled']=True
        output=self.render()
        self.assertIn('Выключен:',output)
        self.assertIn('ssh -6 -p 2223 root@200:1111:2222:3333:4444:5555:6666:7777',output)
        self.assertIn('Разрешённые источники подключения:',output)
        self.assertIn('300:aaaa:bbbb:cccc::/64',output)
        self.state.update(ssh=False,access_mode='subscription',allowed=[])
        output=self.render()
        self.assertIn('без списка IP',output)
        self.assertIn('Доступ через Yggdrasil выключен',output)
        self.assertNotIn('ssh -6',output)

    def test_missing_snapshot_has_an_actionable_error(self):
        with patch.object(multi.local,'BASE',self.root),patch.object(multi.ui,'screen',None):
            with self.assertRaisesRegex(multi.Error,'Сначала выберите установку'):
                multi.result()


if __name__=='__main__': unittest.main()
