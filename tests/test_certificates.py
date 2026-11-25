from pathlib import Path
import json
import os
from contextlib import nullcontext
import subprocess
import shutil
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch
from certificate_runtime import Core, Listener
from certificates import CertificatePair, configured_targets, configured_pair, discover, managed_targets, remember_runtime, setup, sync, validate
from api import Error


class CertificateDiscoveryTests(unittest.TestCase):
    def test_validation_rejects_expired_certificate_and_wrong_key_before_setup(self):
        with tempfile.TemporaryDirectory() as directory:
            cert=Path(directory)/'fullchain.pem'; key=Path(directory)/'privkey.pem'
            cert.touch(); key.touch()
            public='-----BEGIN PUBLIC KEY-----\nfixture\n-----END PUBLIC KEY-----'
            details=public+'\nnotBefore=Nov 30 00:00:00 2000 GMT\nnotAfter=Nov 30 00:00:00 2001 GMT\nHostname node.example.com does match certificate'
            with patch('certificates.run',return_value=details):
                with self.assertRaisesRegex(Error,'просрочен'): validate(cert,key,'node.example.com')
            details=details.replace('2001 GMT','2031 GMT')
            with patch('certificates.run',side_effect=[details,'another public key']):
                with self.assertRaisesRegex(Error,'не образуют пару'): validate(cert,key,'node.example.com')

    @unittest.skipUnless(shutil.which('openssl'),'OpenSSL is exercised in Linux CI')
    def test_relative_live_path_becomes_absolute_without_freezing_archive(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            root=Path(directory); archive=root/'archive'; archive.mkdir()
            cert=archive/'fullchain1.pem'; key=archive/'privkey1.pem'
            subprocess.run(['openssl','req','-x509','-newkey','ec','-pkeyopt','ec_paramgen_curve:prime256v1',
                            '-nodes','-days','1','-subj','/CN=node.example.com',
                            '-keyout',str(key),'-out',str(cert)],check=True,capture_output=True)
            live=root/'live'; live.mkdir()
            (live/'fullchain.pem').symlink_to(cert); (live/'privkey.pem').symlink_to(key)
            pair=validate((live/'fullchain.pem').relative_to(Path.cwd()),(live/'privkey.pem').relative_to(Path.cwd()),'node.example.com')
            self.assertTrue(pair.cert.is_absolute()); self.assertEqual(pair.cert,live/'fullchain.pem')
            self.assertEqual(pair.key,live/'privkey.pem')
    @unittest.skipUnless(shutil.which('openssl'),'OpenSSL is exercised in Linux CI')
    def test_real_openssl_wildcard_in_differently_named_lineage(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); lineage=root/'different-lineage-0001'; lineage.mkdir()
            cert=lineage/'fullchain.pem'; key=lineage/'privkey.pem'
            subprocess.run(['openssl','req','-x509','-newkey','ec','-pkeyopt','ec_paramgen_curve:prime256v1',
                            '-nodes','-days','1','-subj','/CN=*.example.com','-addext','subjectAltName=DNS:*.example.com',
                            '-keyout',str(key),'-out',str(cert)],check=True,capture_output=True)
            self.assertEqual(discover('node.example.com',root).cert,cert)
            self.assertIsNone(discover('deep.node.example.com',root))
    def check(self,*args,**kwargs):
        public='-----BEGIN PUBLIC KEY-----\nfixture\n-----END PUBLIC KEY-----'
        if args[1]=='pkey': return public
        folder=Path(args[args.index('-in')+1]).parent.name
        match='does NOT match' if folder=='wrong-domain' else 'does match'
        year='2000' if folder=='expired' else '2031' if folder.endswith('-0001') else '2030'
        return public+f'\nHostname node.example.com {match} certificate\nnotBefore=Nov 30 00:00:00 1999 GMT\nnotAfter=Nov 30 00:00:00 {year} GMT'

    def test_wildcard_parent_domain_and_renewal_suffix(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            for folder in ('example.com','example.com-0001','wrong-domain','expired','missing-key'):
                path=root/folder; path.mkdir(); (path/'fullchain.pem').touch()
                if folder!='missing-key': (path/'privkey.pem').touch()
            with patch('certificates.run',side_effect=self.check):
                selected=discover('node.example.com',root)
            self.assertEqual((selected.cert,selected.key),(root/'example.com-0001/fullchain.pem',root/'example.com-0001/privkey.pem'))

    def test_no_matching_certificate_does_not_select_first_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'wrong-domain'; path.mkdir()
            (path/'fullchain.pem').touch(); (path/'privkey.pem').touch()
            with patch('certificates.run',side_effect=self.check): self.assertIsNone(discover('node.example.com',Path(directory)))

    def test_caddy_candidate_contents_are_checked_and_bad_key_does_not_mask_valid_pair(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            candidates=[]
            for name in ('wrong-domain','bad-key','caddy-valid'):
                folder=root/name;folder.mkdir()
                cert=folder/'untrusted-name.crt';key=folder/'untrusted-name.key'
                cert.touch();key.touch();candidates.append((cert,key))
            def check(*args,**kwargs):
                if args[1]=='pkey' and Path(args[args.index('-in')+1]).parent.name=='bad-key':return 'wrong key'
                return self.check(*args,**kwargs)
            with patch('certificates.Path.glob',return_value=[]), \
                 patch('certificates.caddy_candidates',return_value=candidates), \
                 patch('certificates.run',side_effect=check):
                selected=discover('node.example.com')
            self.assertEqual((selected.cert,selected.key),candidates[-1])


class CertificateCoexistenceTests(unittest.TestCase):
    def setUp(self):
        if os.name!='posix':
            # Production sync is Linux-only; mock flock for filesystem unit tests.
            lock=patch.dict('sys.modules',{'fcntl':SimpleNamespace(flock=Mock(),LOCK_EX=2,LOCK_NB=4)})
            lock.start();self.addCleanup(lock.stop)
        self.listeners=frozenset({Listener('tcp','00000000',443)})

    def test_existing_inbound_uses_its_actual_longest_mount_and_does_not_write(self):
        inbound={'streamSettings':{'tlsSettings':{'certificates':[{
            'certificateFile':'/certs/node/fullchain.pem','keyFile':'/certs/node/privkey.pem'}]}}}
        mounts=[{'Source':'/unrelated','Destination':'/certs'},
                {'Source':'/etc/letsencrypt/live/node-0001','Destination':'/certs/node','RW':False}]
        pair=CertificatePair(Path('/etc/letsencrypt/live/node-0001/fullchain.pem'),
                             Path('/etc/letsencrypt/live/node-0001/privkey.pem'),'node.example.com',time.time()+864000)
        with patch('certificates.validate',return_value=pair) as checked, patch('certificates.write') as written:
            self.assertEqual(configured_pair(configured_targets(inbound),mounts,'node.example.com'),pair)
        checked.assert_called_once_with(pair.cert,pair.key,'node.example.com')
        written.assert_not_called()

    def test_existing_certificate_without_a_host_mount_is_rejected_before_setup(self):
        inbound={'streamSettings':{'tlsSettings':{'certificates':[{
            'certificateFile':'/private/fullchain.pem','keyFile':'/private/privkey.pem'}]}}}
        with self.assertRaisesRegex(Error,'mount'):
            configured_pair(configured_targets(inbound),[{'Source':'/dev/shm','Destination':'/dev/shm'}],'node.example.com')

    def test_new_writer_uses_dedicated_names_and_preserves_upstream_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            upstream_cert=root/'hysteria_cert.pem'; upstream_key=root/'hysteria_key.pem'
            upstream_cert.write_text('upstream cert'); upstream_key.write_text('upstream key')
            cert=root/'source-cert.pem'; key=root/'source-key.pem'
            cert.write_text('new cert'); key.write_text('new key')
            target_cert=root/'remna-ygg-hy2-cert.pem'; target_key=root/'remna-ygg-hy2-key.pem'
            (root/'cert-sync.json').write_text(json.dumps({'cert':str(cert),'key':str(key),
                'domain':'node.example.com','cert_target':str(target_cert),'key_target':str(target_key)}))
            pair=CertificatePair(cert,key,'node.example.com',time.time()+864000)
            with patch('certificates.BASE',root), patch('certificates.validate',return_value=pair), \
                 patch('certificates.core_snapshot',return_value=Core(0,0,self.listeners)), patch('certificates.os.fchmod',create=True), \
                 patch('certificates.restart_and_wait') as restart:
                sync()
                restart.assert_called_once_with((),saved_listeners=self.listeners)
                restart.reset_mock()
                sync()
                restart.assert_not_called()
            self.assertEqual(target_cert.read_text(),'new cert'); self.assertEqual(target_key.read_text(),'new key')
            self.assertEqual(upstream_cert.read_text(),'upstream cert'); self.assertEqual(upstream_key.read_text(),'upstream key')

    def test_near_expiry_warns_and_unknown_uid_preserves_existing_targets(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);cert=root/'cert';key=root/'key';target=root/'target'
            cert.write_text('source cert');key.write_text('source key');target.write_text('existing cert')
            (root/'cert-sync.json').write_text(json.dumps({'cert':str(cert),'key':str(key),
                'domain':'node.example.com','cert_target':str(target),'key_target':str(root/'target-key')}))
            pair=CertificatePair(cert,key,'node.example.com',time.time()+6*86400)
            with patch('certificates.BASE',root), patch('certificates.validate',return_value=pair), \
                 patch('certificates.core_snapshot',side_effect=Error('UID неизвестен')), \
                 patch('certificates.say') as say, patch('certificates.restart_and_wait') as restart:
                with self.assertRaisesRegex(Error,'UID'):sync()
            self.assertEqual(target.read_text(),'existing cert')
            self.assertFalse((root/'target-key').exists())
            self.assertIn('7 дней',say.call_args.args[0]);restart.assert_not_called()

    def test_sync_installs_exact_validated_snapshot_even_if_source_rotates(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);cert=root/'cert';key=root/'key'
            cert.write_text('verified cert');key.write_text('verified key')
            (root/'cert-sync.json').write_text(json.dumps({'cert':str(cert),'key':str(key),
                'cert_target':str(root/'target-cert'),'key_target':str(root/'target-key')}))
            def rotate(snapshot_cert,snapshot_key,domain):
                self.assertEqual(snapshot_cert.read_text(),'verified cert')
                self.assertEqual(snapshot_key.read_text(),'verified key')
                cert.write_text('next cert');key.write_text('next key')
                return CertificatePair(snapshot_cert,snapshot_key,domain,time.time()+864000)
            with patch('certificates.BASE',root), patch('certificates.validate',side_effect=rotate), \
                 patch('certificates.core_snapshot',return_value=Core(0,0,self.listeners)), \
                 patch('certificates.os.fchmod',create=True), patch('certificates.restart_and_wait'):
                sync()
            self.assertEqual((root/'target-cert').read_text(),'verified cert')
            self.assertEqual((root/'target-key').read_text(),'verified key')

    def test_cold_restore_requires_previously_verified_listeners(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);cert=root/'cert';key=root/'key'
            cert.write_text('cert');key.write_text('key')
            config={'cert':str(cert),'key':str(key),'cert_target':str(root/'target-cert'),
                    'key_target':str(root/'target-key'),'udp_ports':[443]}
            state=root/'cert-sync.json';state.write_text(json.dumps(config))
            pair=CertificatePair(cert,key,None,time.time()+864000)
            with patch('certificates.BASE',root), patch('certificates.validate',return_value=pair), \
                 patch('certificates.core_snapshot',return_value=None), \
                 patch('certificates.os.fchmod',create=True), patch('certificates.restart_and_wait') as restart:
                with self.assertRaisesRegex(Error,'нет сохранённой'):sync()
                self.assertFalse((root/'target-key').exists());restart.assert_not_called()
                udp=Listener('udp','00000000',443)
                config['listeners']=[dict(protocol=udp.protocol,address=udp.address,port=udp.port)]
                state.write_text(json.dumps(config));sync()
                restart.assert_called_once_with((443,),saved_listeners=frozenset({udp}))
                restart.reset_mock()
                # A deliberately stopped node with intact files stays stopped.
                sync();restart.assert_not_called()
                # Xray may be visible but still missing listeners during boot.
                (root/'target-key').unlink();restart.reset_mock()
                with patch('certificates.core_snapshot',return_value=Core(0,0,self.listeners)):
                    sync()
                restart.assert_called_once_with((443,),saved_listeners=self.listeners|{udp})
            self.assertEqual((root/'target-key').read_text(),'key')
            self.assertFalse(json.loads(state.read_text())['restart_pending'])

    def test_failed_restart_is_retried_without_weakening_saved_listeners(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);cert=root/'cert';key=root/'key'
            cert.write_text('cert');key.write_text('key')
            udp=Listener('udp','00000000',443);listeners=self.listeners|{udp}
            state=root/'cert-sync.json'
            state.write_text(json.dumps({'cert':str(cert),'key':str(key),'cert_target':str(root/'target-cert'),
                'key_target':str(root/'target-key'),'udp_ports':[443],
                'listeners':[dict(protocol=l.protocol,address=l.address,port=l.port) for l in listeners]}))
            pair=CertificatePair(cert,key,None,time.time()+864000)
            with patch('certificates.BASE',root), patch('certificates.validate',return_value=pair), \
                 patch('certificates.core_snapshot',return_value=Core(0,0,listeners)) as snapshot, \
                 patch('certificates.os.fchmod',create=True), patch('certificates.restart_and_wait',side_effect=Error('startup')) as restart:
                with self.assertRaisesRegex(Error,'startup'):sync()
                self.assertTrue(json.loads(state.read_text())['restart_pending'])
                snapshot.return_value=Core(0,0,self.listeners);restart.side_effect=None;restart.reset_mock()
                sync()
                restart.assert_called_once_with((443,),saved_listeners=listeners)
            self.assertFalse(json.loads(state.read_text())['restart_pending'])

    def test_manual_udp_port_change_does_not_erase_verified_expectation(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);cert=root/'cert';key=root/'key'
            cert.write_text('cert');key.write_text('key')
            target_cert=root/'target-cert';target_key=root/'target-key'
            target_cert.write_text('cert');target_key.write_text('key')
            config={'cert':str(cert),'key':str(key),'udp_ports':[443],
                    'cert_target':str(target_cert),'key_target':str(target_key),
                    'listeners':[dict(protocol='udp',address='00000000',port=443)]}
            state=root/'cert-sync.json';original=json.dumps(config);state.write_text(original)
            pair=CertificatePair(cert,key,None,time.time()+864000)
            with patch('certificates.BASE',root), patch('certificates.validate',return_value=pair), \
                 patch('certificates.core_snapshot',return_value=Core(0,0,self.listeners)):
                with self.assertRaisesRegex(Error,'UDP-порт'):sync()
            self.assertEqual(state.read_text(),original)

    def test_runtime_persistence_waits_for_selected_udp_to_start(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);state=root/'cert-sync.json';state.write_text(json.dumps({'udp_ports':[443]}))
            udp=Listener('udp','00000000',443)
            with patch('certificates.BASE',root), patch('certificates.time.sleep') as sleep, \
                 patch('certificates.core_snapshot',side_effect=[Core(0,0,self.listeners),Core(0,0,self.listeners|{udp})]):
                remember_runtime()
            sleep.assert_called_once_with(2)
            self.assertIn(dict(protocol='udp',address='00000000',port=443),json.loads(state.read_text())['listeners'])

    def test_setup_records_dedicated_targets_and_legacy_writer_is_identified(self):
        pair=CertificatePair(Path('/certs/fullchain.pem'),Path('/certs/privkey.pem'),'node.example.com',time.time()+864000)
        with patch('certificates.write') as written, patch('certificates._sync_locked'), \
             patch('certificates.certificate_lock',return_value=nullcontext()), patch('certificates.run'):
            setup(pair,udp_port=8443)
        state=json.loads(next(call.args[1] for call in written.call_args_list if call.args[0].name=='cert-sync.json'))
        self.assertEqual(Path(state['cert_target']),Path('/dev/shm/remna-ygg-hy2-cert.pem'))
        self.assertEqual(Path(state['key_target']),Path('/dev/shm/remna-ygg-hy2-key.pem'))
        self.assertEqual(state['udp_ports'],[8443])
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            with patch('certificates.BASE',root):
                self.assertIsNone(managed_targets())
                (root/'cert-sync.json').write_text(json.dumps({'cert':'/certs/fullchain.pem','key':'/certs/privkey.pem'}))
                self.assertEqual(managed_targets(),(Path('/dev/shm/hysteria_cert.pem'),Path('/dev/shm/hysteria_key.pem')))
                (root/'cert-sync.json').write_text(json.dumps(state))
                self.assertEqual(managed_targets(),(Path(state['cert_target']),Path(state['key_target'])))


if __name__=='__main__': unittest.main()
