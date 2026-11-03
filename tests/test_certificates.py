from pathlib import Path
import subprocess
import shutil
import tempfile
import unittest
from unittest.mock import patch
from certificates import discover, validate
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

if __name__=='__main__': unittest.main()
