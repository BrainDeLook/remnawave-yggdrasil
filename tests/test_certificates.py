from pathlib import Path
import json
import subprocess
import shutil
import tempfile
import unittest
from unittest.mock import patch
from certificates import CertificatePair, configured_targets, configured_pair, discover, managed_targets, setup, sync, validate
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
            self.assertEqual(discover('node.example.com',root),(cert,key))
            self.assertIsNone(discover('deep.node.example.com',root))
    def check(self,args,**kwargs):
        folder=Path(args[args.index('-in')+1]).parent.name
        if folder=='wrong-domain': return subprocess.CompletedProcess(args,0,'Hostname node.example.com does NOT match certificate\nnotAfter=Nov 30 00:00:00 2030 GMT','')
        year='2000' if folder=='expired' else '2031' if folder.endswith('-0001') else '2030'
        return subprocess.CompletedProcess(args,0,f'Hostname node.example.com does match certificate\nnotAfter=Nov 30 00:00:00 {year} GMT','')
    def test_wildcard_parent_domain_and_renewal_suffix(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            for folder in ('example.com','example.com-0001','wrong-domain','expired','missing-key'):
                path=root/folder; path.mkdir(); (path/'fullchain.pem').touch()
                if folder!='missing-key': (path/'privkey.pem').touch()
            with patch('certificates.subprocess.run',side_effect=self.check):
                self.assertEqual(discover('node.example.com',root),(root/'example.com-0001/fullchain.pem',root/'example.com-0001/privkey.pem'))
    def test_no_matching_certificate_does_not_select_first_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'wrong-domain'; path.mkdir()
            (path/'fullchain.pem').touch(); (path/'privkey.pem').touch()
            with patch('certificates.subprocess.run',side_effect=self.check): self.assertIsNone(discover('node.example.com',Path(directory)))

class CertificateCoexistenceTests(unittest.TestCase):
    def test_existing_inbound_uses_its_actual_longest_mount_and_does_not_write(self):
        inbound={'streamSettings':{'tlsSettings':{'certificates':[{
            'certificateFile':'/certs/node/fullchain.pem','keyFile':'/certs/node/privkey.pem'}]}}}
        mounts=[{'Source':'/unrelated','Destination':'/certs'},
                {'Source':'/etc/letsencrypt/live/node-0001','Destination':'/certs/node','RW':False}]
        pair=CertificatePair(Path('/etc/letsencrypt/live/node-0001/fullchain.pem'),
                             Path('/etc/letsencrypt/live/node-0001/privkey.pem'),'node.example.com')
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
            with patch('certificates.BASE',root), patch('certificates.validate'), \
                 patch('certificates.os.fchmod',create=True), patch('certificates.run') as run:
                sync()
                run.assert_called_once_with('docker','restart','remnanode',timeout=120)
                run.reset_mock()
                sync()
                run.assert_not_called()
            self.assertEqual(target_cert.read_text(),'new cert'); self.assertEqual(target_key.read_text(),'new key')
            self.assertEqual(upstream_cert.read_text(),'upstream cert'); self.assertEqual(upstream_key.read_text(),'upstream key')

    def test_setup_records_dedicated_targets_and_legacy_writer_is_identified(self):
        pair=CertificatePair(Path('/certs/fullchain.pem'),Path('/certs/privkey.pem'),'node.example.com')
        with patch('certificates.write') as written, patch('certificates.sync'), patch('certificates.run'):
            setup(pair)
        state=json.loads(written.call_args_list[0].args[1])
        self.assertEqual(Path(state['cert_target']),Path('/dev/shm/remna-ygg-hy2-cert.pem'))
        self.assertEqual(Path(state['key_target']),Path('/dev/shm/remna-ygg-hy2-key.pem'))
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            with patch('certificates.BASE',root):
                self.assertIsNone(managed_targets())
                (root/'cert-sync.json').write_text(json.dumps({'cert':'/certs/fullchain.pem','key':'/certs/privkey.pem'}))
                self.assertEqual(managed_targets(),(Path('/dev/shm/hysteria_cert.pem'),Path('/dev/shm/hysteria_key.pem')))
                (root/'cert-sync.json').write_text(json.dumps(state))
                self.assertEqual(managed_targets(),(Path(state['cert_target']),Path(state['key_target'])))


if __name__=='__main__': unittest.main()
