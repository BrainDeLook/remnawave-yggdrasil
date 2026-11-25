import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from api import Error
from certificate_sources import caddy_candidates


class CaddyDiscoveryTests(unittest.TestCase):
    def test_actual_readonly_mount_multiple_authorities_and_stable_file_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            pairs = []
            for authority, domain in (('acme-v02.api.letsencrypt.org-directory', 'node.example.com'),
                                      ('acme.zerossl.com-v2-DV90', 'wildcard_.example.com'),
                                      ('other-ca', 'unrelated.example.org')):
                folder = root / 'caddy' / 'certificates' / authority / domain
                folder.mkdir(parents=True)
                cert = folder / (domain + '.crt'); key = cert.with_suffix('.key')
                cert.write_text('certificate fixture'); key.write_text('private key fixture')
                pairs.append((cert, key))
            (pairs[0][0].parent / 'missing-key.crt').touch()
            containers = '\n'.join(json.dumps(c) for c in (
                {'ID': 'caddy1', 'Names': 'caddy-remnawave', 'Image': 'custom-caddy'},
                {'ID': 'caddy2', 'Names': 'another-name', 'Image': 'docker.io/library/caddy:2'},
                {'ID': 'other', 'Names': 'unrelated', 'Image': 'nginx:latest'}))
            mounts = json.dumps([{'Source': str(root), 'Destination': '/data', 'RW': False}])
            with patch('certificate_sources.shutil.which', return_value='/usr/bin/docker'), \
                 patch('certificate_sources.run', side_effect=[containers, mounts, mounts]) as run:
                self.assertEqual(caddy_candidates('node.example.com'), sorted(pairs))
            self.assertEqual(run.call_count, 3)
            self.assertTrue(all(call.args[0] == 'docker' for call in run.call_args_list))
            self.assertTrue(all('--format' in call.args for call in run.call_args_list))
            self.assertEqual(pairs[0][0].read_text(), 'certificate fixture')
            self.assertEqual(pairs[0][1].read_text(), 'private key fixture')

    def test_named_volume_source_is_taken_from_container_mount(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            folder = root / 'caddy/certificates/ca/node.example.com'
            folder.mkdir(parents=True)
            cert = folder / 'node.example.com.crt'; key = cert.with_suffix('.key')
            cert.touch(); key.touch()
            container = json.dumps({'ID': 'caddy', 'Names': 'remnawave-caddy', 'Image': 'caddy@sha256:fixture'})
            mount = json.dumps([{'Type': 'volume', 'Name': 'project_custom_data',
                                 'Destination': '/data', 'Source': str(root)}])
            with patch('certificate_sources.shutil.which', return_value='docker'), \
                 patch('certificate_sources.run', side_effect=[container, mount]):
                self.assertEqual(caddy_candidates('node.example.com'), [(cert, key)])

    def test_absent_docker_or_no_caddy_returns_no_candidates(self):
        with patch('certificate_sources.shutil.which', return_value=None), \
             patch('certificate_sources.run') as run:
            self.assertEqual(caddy_candidates('node.example.com'), [])
            run.assert_not_called()
        for response in ('', json.dumps({'ID': 'web', 'Names': 'web', 'Image': 'nginx:latest'})):
            with patch('certificate_sources.shutil.which', return_value='docker'), \
                 patch('certificate_sources.run', return_value=response) as run:
                self.assertEqual(caddy_candidates('node.example.com'), [])
                self.assertEqual(run.call_count, 1)

    def test_malformed_caddy_mount_is_rejected(self):
        container = json.dumps({'ID': 'caddy', 'Names': 'remnawave-caddy', 'Image': 'caddy:2'})
        for mounts in ('{}', '[null]', '[{"Destination":"/data","Source":"relative"}]',
                       '[{"Destination":"/data","Source":null}]'):
            with self.subTest(mounts=mounts), \
                 patch('certificate_sources.shutil.which', return_value='docker'), \
                 patch('certificate_sources.run', side_effect=[container, mounts]):
                with self.assertRaises(Error):
                    caddy_candidates('node.example.com')


if __name__ == '__main__': unittest.main()
