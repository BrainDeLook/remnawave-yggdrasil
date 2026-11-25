"""Selected Hysteria2 certificate copy, boot/renewal sync; no world-readable key."""
import json
import os
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
import posixpath
import re
import ssl
import tempfile
import time
from api import Error
from certificate_sources import caddy_candidates
from certificate_runtime import Listener, core_snapshot, restart_and_wait
from ui import say
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
    expires_at: float


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
    return CertificatePair(cert,key,domain,ssl.cert_time_to_seconds(dates['notAfter']))


def discover(domain: str, root: Path | None = None) -> CertificatePair | None:
    """Validate filesystem candidates once; retain live paths for future renewals."""
    live=root if root is not None else Path('/etc/letsencrypt/live')
    candidates=[(cert,cert.parent/'privkey.pem') for cert in sorted(live.glob('*/fullchain.pem'))]
    if root is None: candidates.extend(caddy_candidates(domain))
    matches=[]
    for cert,key in dict.fromkeys(candidates):
        if not cert.is_file() or not key.is_file(): continue
        try:
            pair=validate(cert,key,domain)
        except Error:
            # Candidates can belong to other domains/issuers or be mid-renewal.
            # A bad candidate must not hide a valid pair from another source.
            continue
        matches.append(pair)
    return max(matches,key=lambda pair:(pair.expires_at,str(pair.cert))) if matches else None


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


@contextmanager
def certificate_lock():
    import fcntl
    # A renewal timer and an installer must not interleave certificate pairs.
    with (BASE/'cert-sync.lock').open('w') as lock:
        try:
            fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise Error('Сертификат Hysteria2 уже обновляется другим запуском. Повторите позже.') from exc
        yield


def sync():
    with certificate_lock():
        _sync_locked()


def _sync_locked():
    config=json.loads((BASE/'cert-sync.json').read_text())
    # Certbot/Caddy can replace source files during this read. Validate the exact
    # snapshot we will install; a mixed renewal pair fails before any target write.
    contents={key:Path(config[key]).read_bytes() for key in ('cert','key')}
    with tempfile.TemporaryDirectory(prefix='remna-hy2-check-') as directory:
        cert=Path(directory)/'certificate.pem';key=Path(directory)/'private-key.pem'
        cert.write_bytes(contents['cert']);key.write_bytes(contents['key']);key.chmod(0o600)
        pair=validate(cert,key,config.get('domain'))
    if pair.expires_at-time.time()<7*86400:
        say('  Сертификат Hysteria2 истекает менее чем через 7 дней. Проверьте автопродление Certbot/Caddy.','warning')
    targets=[];missing=False
    for key,destination,mode in (('cert',config.get('cert_target',LEGACY_CERT_TARGET),0o644),
                                 ('key',config.get('key_target',LEGACY_KEY_TARGET),0o600)):
        target=Path(destination)
        if target.is_symlink(): raise Error('Путь сертификата /dev/shm занят символической ссылкой')
        absent=not target.exists();missing=missing or absent
        targets.append((key,target,mode,absent or target.read_bytes()!=contents[key]))
    changed=any(different for key,target,mode,different in targets)
    core=core_snapshot()
    saved=frozenset(Listener(**value) for value in config.get('listeners',[]))
    pending=config.get('restart_pending',False)
    restoring=missing and bool(saved)
    if core is not None:
        if core.effective_uid!=0 or core.filesystem_uid!=0:
            raise Error('Процесс rw-core/Xray работает не от root; ключ 600 ему недоступен. Права 644 не применяются.')
        udp_ports=config.get('udp_ports')
        if udp_ports is None:
            state=BASE/'multi-state.json'
            hosts=json.loads(state.read_text()).get('hosts',[]) if state.exists() else []
            udp_ports=[host['port'] for host in hosts if host['protocol']=='hy2']
        live=frozenset(listener for listener in core.listeners
                       if listener.protocol.startswith('tcp') or listener.port in udp_ports)
        if not (pending or restoring) and any(l.protocol.startswith('udp') for l in saved) and not set(udp_ports)<={
                l.port for l in live if l.protocol.startswith('udp')}:
            raise Error('У rw-core/Xray отсутствует настроенный UDP-порт Hysteria2. Если порт изменён в панели, повторите настройку установщиком; сохранённые слушатели не менялись.')
        saved=saved|live if pending or restoring else live
        if not saved: raise Error('Не удалось определить слушатели rw-core/Xray до настройки сертификата')
        config['udp_ports']=udp_ports
        config['listeners']=[dict(protocol=l.protocol,address=l.address,port=l.port)
                             for l in sorted(saved,key=lambda l:(l.protocol,l.address,l.port))]
    elif not saved:
        raise Error('Процесс rw-core/Xray не найден и нет сохранённой проверки слушателей. Запустите ноду и повторите настройку; сертификат не менялся.')
    config['restart_pending']=pending or changed
    write(BASE/'cert-sync.json',json.dumps(config))
    for key,target,mode,different in targets:
        content=contents[key]
        if different:
            # /dev/shm is shared and writable: a predictable .new filename could be
            # planted as a symlink. Create our own 0600 file and replace atomically.
            with tempfile.NamedTemporaryFile(dir=target.parent,prefix='.remna-hy2-',delete=False) as temporary:
                os.fchmod(temporary.fileno(),mode)
                temporary.write(content)
            os.replace(temporary.name,target)
        target.chmod(mode)
    if config['restart_pending']:
        restart_and_wait(tuple(config.get('udp_ports',[])),saved_listeners=saved)
        config['restart_pending']=False
        write(BASE/'cert-sync.json',json.dumps(config))


def remember_runtime():
    """Persist the applied core's listeners for recovery after tmpfs is cleared."""
    with certificate_lock():
        _remember_runtime_locked()


def _remember_runtime_locked():
    config=json.loads((BASE/'cert-sync.json').read_text())
    ports=config['udp_ports']
    deadline=time.monotonic()+60
    while True:
        core=core_snapshot()
        if core is not None:
            if core.effective_uid!=0 or core.filesystem_uid!=0:
                raise Error('Не удалось подтвердить root-процесс rw-core/Xray после применения Hysteria2')
            if set(ports)<={l.port for l in core.listeners if l.protocol.startswith('udp')}: break
        if time.monotonic()>=deadline:
            raise Error('После применения не появился UDP-слушатель Hysteria2 у процесса rw-core/Xray за 60 секунд')
        time.sleep(2)
    config['listeners']=[dict(protocol=l.protocol,address=l.address,port=l.port)
                         for l in sorted(core.listeners,key=lambda l:(l.protocol,l.address,l.port))
                         if l.protocol.startswith('tcp') or l.port in ports]
    write(BASE/'cert-sync.json',json.dumps(config))


def setup(pair: CertificatePair, cert_target: Path = CERT_TARGET, key_target: Path = KEY_TARGET, *, udp_port: int):
    if cert_target.parent != Path('/dev/shm') or key_target.parent != Path('/dev/shm') or cert_target == key_target:
        raise Error('Назначение сертификата Hysteria2 должно быть отдельной парой файлов в /dev/shm')
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
    with certificate_lock():
        write(BASE/'cert-sync.json',json.dumps({'cert':str(pair.cert),'key':str(pair.key),'domain':pair.domain,
                                            'cert_target':str(cert_target),'key_target':str(key_target),'udp_ports':[udp_port]}))
        _sync_locked()
    run('systemctl','daemon-reload')
    run('systemctl','enable','--now','remna-hy2-certs.timer')
