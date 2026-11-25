"""Read-only certificate candidates from Caddy's actual Docker data mounts."""
import json
from pathlib import Path
import shutil

from api import Error
from node import run


def caddy_candidates(domain: str) -> list[tuple[Path, Path]]:
    """Return file pairs; the caller must validate their contents against domain."""
    if not shutil.which('docker'):
        return []
    containers = run('docker', 'ps', '--all', '--format', '{{json .}}', check=False)
    candidates = set()
    for line in containers.splitlines():
        try:
            container = json.loads(line)
        except json.JSONDecodeError as exc:
            raise Error('Docker вернул некорректный список контейнеров при поиске сертификата Caddy') from exc
        if not isinstance(container, dict) or any(not isinstance(container.get(key), str)
                                                  for key in ('ID', 'Names', 'Image')):
            raise Error('Docker: в списке контейнеров отсутствуют ID, Names или Image')
        image = container['Image'].rsplit('/', 1)[-1].split('@', 1)[0].split(':', 1)[0]
        if container['Names'] not in ('caddy-remnawave', 'remnawave-caddy') and image != 'caddy':
            continue
        raw = run('docker', 'inspect', '--format', '{{json .Mounts}}', container['ID'], check=False)
        if not raw.strip():
            continue  # The container may have disappeared since docker ps.
        try:
            mounts = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise Error('Docker вернул некорректные mount при поиске сертификата Caddy') from exc
        if not isinstance(mounts, list):
            raise Error('Docker: ожидался список mount контейнера Caddy')
        for mount in mounts:
            if not isinstance(mount, dict) or not isinstance(mount.get('Destination'), str):
                raise Error('Docker: mount контейнера Caddy не содержит Destination')
            if mount['Destination'] != '/data':
                continue
            source = mount.get('Source')
            if not isinstance(source, str) or not Path(source).is_absolute():
                raise Error('Docker: /data контейнера Caddy не содержит абсолютный Source')
            storage = Path(source) / 'caddy' / 'certificates'
            for cert in storage.glob('*/*/*.crt'):
                key = cert.with_suffix('.key')
                if cert.is_file() and key.is_file():
                    candidates.add((cert, key))
    return sorted(candidates)
