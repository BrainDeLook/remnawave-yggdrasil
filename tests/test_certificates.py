from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from certificates import discover


class CertificateDiscoveryTests(unittest.TestCase):
    def check(self,args,**kwargs):
        folder=Path(args[args.index('-in')+1]).parent.name
        if folder=='wrong-domain': return subprocess.CompletedProcess(args,0,'Hostname node.example.com does NOT match certificate\nnotAfter=Nov 30 00:00:00 2030 GMT','')
        if folder=='expired': return subprocess.CompletedProcess(args,1,'Certificate will expire','')
        year='2031' if folder.endswith('-0001') else '2030'
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
