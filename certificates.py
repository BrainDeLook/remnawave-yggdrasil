"""Selected Hysteria2 certificate copy, boot/renewal sync; no world-readable key."""
import json
import os
from pathlib import Path
import shutil
import ssl
import subprocess
import tempfile
from api import Error
from node import APP, BASE, run, write


def discover(domain: str, root: Path = Path('/etc/letsencrypt/live')) -> tuple[Path,Path] | None:
    """Match certificate contents, not Certbot's directory/lineage name."""
    matches=[]
    for cert in sorted(root.glob('*/fullchain.pem')):
        key=cert.parent/'privkey.pem'
        if not cert.is_file() or not key.is_file(): continue
        checked=subprocess.run(['openssl','x509','-in',str(cert),'-noout','-checkhost',domain,
                                '-checkend','0','-enddate'],capture_output=True,text=True,timeout=10)
        if checked.returncode or 'does match certificate' not in checked.stdout: continue
        expiry=next((line.removeprefix('notAfter=') for line in checked.stdout.splitlines() if line.startswith('notAfter=')),None)
        if expiry:
            matches.append((ssl.cert_time_to_seconds(expiry),str(cert),cert,key))
    if not matches: return None
    # Prefer the longest-lived valid certificate, retaining the stable live symlink
    # so the existing renewal mechanism continues to work.
    selected=max(matches)
    return selected[2],selected[3]


def sync():
    config=json.loads((BASE/'cert-sync.json').read_text())
    changed=False
    for key,destination,mode in (('cert','/dev/shm/hysteria_cert.pem',0o644),('key','/dev/shm/hysteria_key.pem',0o600)):
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


def setup(cert: Path, key: Path):
    if not cert.is_file() or not key.is_file(): raise Error('Не найдены файлы сертификата Hysteria2')
    run('openssl','x509','-in',str(cert),'-noout','-checkend','0')
    cert_public=run('openssl','x509','-in',str(cert),'-pubkey','-noout')
    key_public=run('openssl','pkey','-in',str(key),'-pubout')
    if cert_public.strip()!=key_public.strip(): raise Error('Ключ и сертификат Hysteria2 не совпадают')
    write(BASE/'cert-sync.json',json.dumps({'cert':str(cert),'key':str(key)}))
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
