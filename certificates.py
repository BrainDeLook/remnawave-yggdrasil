"""Selected Hysteria2 certificate copy, boot/renewal sync; no world-readable key."""
import json
import os
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
import posixpath
import re
import ssl
import subprocess
import tempfile
import time
from api import Error
from node import APP, BASE, run, write

CERT_TARGET = Path('/dev/shm/remna-ygg-hy2-cert.pem')
KEY_TARGET = Path('/dev/shm/remna-ygg-hy2-key.pem')
LEGACY_CERT_TARGET = Path('/dev/shm/hysteria_cert.pem')
LEGACY_KEY_TARGET = Path('/dev/shm/hysteria_key.pem')


@dataclass(frozen=True)
class CertificatePair:
    cert: Path
    key: Path
    domain: str | None


def validate(cert: Path, key: Path, domain: str | None = None) -> CertificatePair:
    # absolute() preserves Certbot's live symlink; resolve() would pin an archive version.
    cert=cert.expanduser().absolute(); key=key.expanduser().absolute()
    if not cert.is_file() or not key.is_file(): raise Error('Не найдены файл сертификата или его приватный ключ')
    details=run('openssl','x509','-in',str(cert),'-noout','-pubkey','-startdate','-enddate',
                *(['-checkhost',domain] if domain else []))
    if domain and 'does match certificate' not in details:
        raise Error('Сертификат Hysteria2 не подходит для домена '+domain)
    dates=dict(line.split('=',1) for line in details.splitlines() if line.startswith(('notBefore=','notAfter=')))
    if 'notBefore' not in dates or 'notAfter' not in dates: raise Error('OpenSSL не вернул срок действия сертификата')
    now=time.time()
    if ssl.cert_time_to_seconds(dates['notBefore'])>now: raise Error('Срок действия сертификата Hysteria2 ещё не начался')
    if ssl.cert_time_to_seconds(dates['notAfter'])<=now: raise Error('Сертификат Hysteria2 просрочен — обновите его перед установкой')
    public=re.search(r'-----BEGIN PUBLIC KEY-----.*?-----END PUBLIC KEY-----',details,re.S)
    if not public: raise Error('Не удалось прочитать публичный ключ сертификата')
    key_public=run('openssl','pkey','-in',str(key),'-pubout')
    if public[0].strip()!=key_public.strip(): raise Error('Сертификат Hysteria2 и приватный ключ не образуют пару')
    return CertificatePair(cert,key,domain)


def discover(domain: str, root: Path = Path('/etc/letsencrypt/live')) -> tuple[Path,Path] | None:
    """Match certificate contents, not Certbot's directory/lineage name."""
    matches=[]
    for cert in sorted(root.glob('*/fullchain.pem')):
        key=cert.parent/'privkey.pem'
        if not cert.is_file() or not key.is_file(): continue
        checked=subprocess.run(['openssl','x509','-in',str(cert),'-noout','-checkhost',domain,
                                '-enddate'],capture_output=True,text=True,timeout=10)
        if checked.returncode or 'does match certificate' not in checked.stdout: continue
        expiry=next((line.removeprefix('notAfter=') for line in checked.stdout.splitlines() if line.startswith('notAfter=')),None)
        if expiry:
            deadline=ssl.cert_time_to_seconds(expiry)
            if deadline>time.time(): matches.append((deadline,str(cert),cert,key))
    if not matches: return None
    # Prefer the longest-lived valid certificate, retaining the stable live symlink
    # so the existing renewal mechanism continues to work.
    selected=max(matches)
    return selected[2],selected[3]


def configured_targets(inbound: dict) -> tuple[PurePosixPath, PurePosixPath]:
    """Validate the existing TLS file paths once at the profile boundary."""
    stream = inbound.get('streamSettings', {})
    tls = stream.get('tlsSettings', {}) if isinstance(stream, dict) else {}
    entries = tls.get('certificates', []) if isinstance(tls, dict) else []
    if not isinstance(entries, list) or len(entries) != 1 or not isinstance(entries[0], dict):
        raise Error('Для проверки существующего Hysteria2 нужен один файловый TLS-сертификат в inbound')
    targets = []
    for key in ('certificateFile', 'keyFile'):
        value = entries[0].get(key)
        if not isinstance(value, str) or not value.startswith('/'):
            raise Error('В существующем Hysteria2 не задан абсолютный путь '+key)
        targets.append(PurePosixPath(posixpath.normpath(value)))
    return targets[0], targets[1]


def configured_pair(targets: tuple[PurePosixPath, PurePosixPath], mounts: list[dict], domain: str) -> CertificatePair:
    """Read mounted certificate files without taking over their renewal."""
    files = []
    for container_path in targets:
        mounted = [m for m in mounts if container_path.is_relative_to(PurePosixPath(m['Destination']))]
        if not mounted:
            raise Error('Файл Hysteria2 не доступен через mount контейнера: '+str(container_path))
        mount = max(mounted, key=lambda m: len(PurePosixPath(m['Destination']).parts))
        files.append(Path(mount['Source']) / container_path.relative_to(PurePosixPath(mount['Destination'])))
    return validate(files[0], files[1], domain)


def managed_targets() -> tuple[Path, Path] | None:
    """Identify our existing writer; releases before 0.2.4 used shared filenames."""
    path = BASE/'cert-sync.json'
    if not path.exists():
        return None
    config = json.loads(path.read_text())
    return Path(config.get('cert_target', LEGACY_CERT_TARGET)), Path(config.get('key_target', LEGACY_KEY_TARGET))


def sync():
    config=json.loads((BASE/'cert-sync.json').read_text())
    # Renewal replaces source files independently of the installer; validate this read.
    validate(Path(config['cert']),Path(config['key']),config.get('domain'))
    changed=False
    for key,destination,mode in (('cert',config.get('cert_target',str(LEGACY_CERT_TARGET)),0o644),
                                 ('key',config.get('key_target',str(LEGACY_KEY_TARGET)),0o600)):
        source=Path(config[key]); target=Path(destination)
        if not source.is_file(): raise Error('Нет файла сертификата для Hysteria2: '+str(source))
        if target.is_symlink(): raise Error('Путь сертификата /dev/shm занят символической ссылкой')
        content=source.read_bytes()
        if not target.exists() or target.read_bytes()!=content:
            # /dev/shm is shared and writable: a predictable .new filename could be
            # planted as a symlink. Create our own 0600 file and replace atomically.
            with tempfile.NamedTemporaryFile(dir=target.parent,prefix='.remna-hy2-',delete=False) as temporary:
                os.fchmod(temporary.fileno(),mode)
                temporary.write(content)
            os.replace(temporary.name,target)
            changed=True
        target.chmod(mode)
    if changed: run('docker','restart','remnanode',timeout=120)


def setup(pair: CertificatePair, cert_target: Path = CERT_TARGET, key_target: Path = KEY_TARGET):
    if cert_target.parent != Path('/dev/shm') or key_target.parent != Path('/dev/shm') or cert_target == key_target:
        raise Error('Назначение сертификата Hysteria2 должно быть отдельной парой файлов в /dev/shm')
    write(BASE/'cert-sync.json',json.dumps({'cert':str(pair.cert),'key':str(pair.key),'domain':pair.domain,
                                        'cert_target':str(cert_target),'key_target':str(key_target)}))
    write(Path('/etc/systemd/system/remna-hy2-certs.service'),f'''[Unit]
Description=Remnanode Hysteria2 certificate sync
After=docker.service
Requires=docker.service
[Service]
Type=oneshot
ExecStart=/usr/bin/python3 {APP}/multi.py --sync-certs
''',0o644)
    write(Path('/etc/systemd/system/remna-hy2-certs.timer'),'''[Unit]
Description=Check Hysteria2 certificate renewal and restore tmpfs after boot
[Timer]
OnBootSec=15s
OnUnitActiveSec=1h
[Install]
WantedBy=timers.target
''',0o644)
    sync()
    run('systemctl','daemon-reload')
    run('systemctl','enable','--now','remna-hy2-certs.timer')
