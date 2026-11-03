"""Node-local setup. Never edits Docker, Xray profiles, default routes or DNS."""
from __future__ import annotations
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import socket
import subprocess
import tempfile
import time
import urllib.request
from api import Error

BASE = Path('/etc/remnawave-yggdrasil')
APP = Path('/opt/remnawave-yggdrasil')
TABLE = 'remnawave_yggdrasil'
YGG_VERSION = '0.5.14'
YGG_HASHES = {'amd64':'72745496b09baa1c3f5a6741e1257a704cfdb9d00cdf3ddbef025987b239699d',
              'arm64':'ac8c9a9a76c54b878fd251aef5a2bbd20375c6109b28b32435683441462360c9'}
PEERS = ['tls://nl1.route172.de:443?key=000000002643ae27bad63e2a81f1d30554ce9ea634f5990062a9e079e23593cd',
         'tls://cirno.nadeko.net:44442','tls://vpn.itrus.su:7992']

def run(*args: str, input: str | bytes | None = None, timeout: int = 90, check: bool = True) -> str:
    result = subprocess.run(args, input=input, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=not isinstance(input, bytes), timeout=timeout)
    out = result.stdout.decode() if isinstance(result.stdout, bytes) else result.stdout
    err = result.stderr.decode(errors='replace') if isinstance(result.stderr, bytes) else result.stderr
    if check and result.returncode:
        # Commands here contain paths/ports only; API credentials never enter argv.
        raise Error(f'{args[0]}: {err.strip()[-1200:] or "операция не выполнена"}')
    return out

def write(path: Path, text: str, mode: int = 0o600):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + '.new')
    tmp.write_text(text, encoding='utf-8')
    tmp.chmod(mode)
    os.replace(tmp, path)

def download(url: str, path: Path, digest: str):
    with urllib.request.urlopen(url, timeout=90) as response:
        content = response.read(150 * 1024 * 1024)
    if hashlib.sha256(content).hexdigest() != digest:
        raise Error('Контрольная сумма загрузки не совпала')
    path.write_bytes(content)


def networks(values: list[str], pair: bool = False, role: str = "any") -> list[str]:
    result = set()
    for value in values:
        try:
            if '%' in value:
                raise ValueError()
            net = ipaddress.IPv6Network(value, strict=True)
        except ValueError:
            raise Error('Некорректный IPv6/префикс: ' + value) from None
        if not net.subnet_of(ipaddress.IPv6Network('200::/7')):
            raise Error('Нужен IPv6 Yggdrasil из 200::/7')
        if net.prefixlen < 128 and not (net.subnet_of(ipaddress.IPv6Network('300::/8')) and net.prefixlen >= 64):
            raise Error('Адрес роутера укажите без /7; LAN — 300:…/64 или более узкая подсеть')
        if role=='router' and not (net.prefixlen==128 and net.subnet_of(ipaddress.IPv6Network('200::/8'))):
            raise Error('В этом поле нужны адреса роутеров 200:… без /7')
        if role=='lan' and not (net.prefixlen==64 and net.subnet_of(ipaddress.IPv6Network('300::/8'))):
            raise Error('В этом поле нужны подсети LAN 300:…/64')
        result.add(str(net.network_address) if net.prefixlen == 128 else str(net))
    if not result:
        raise Error('Список доступа не может быть пустым')
    if pair:
        nets = [ipaddress.IPv6Network(v) for v in result]
        if not any(n.prefixlen == 128 and n.subnet_of(ipaddress.IPv6Network('200::/8')) for n in nets):
            raise Error('Нужен IPv6 роутера 200:… без /7')
        if not any(n.prefixlen == 64 and n.subnet_of(ipaddress.IPv6Network('300::/8')) for n in nets):
            raise Error('Нужна подсеть LAN 300:…/64')
    return sorted(result)


def preflight():
    if os.geteuid() != 0 or not Path('/run/systemd/system').is_dir():
        raise Error('Запустите от root на VPS с systemd')
    release = dict(line.split('=', 1) for line in Path('/etc/os-release').read_text().splitlines() if '=' in line)
    distro = release.get('ID', '').strip('"')
    major = int(release.get('VERSION_ID', '0').strip('"').split('.')[0])
    if (distro == 'debian' and major < 12) or (distro == 'ubuntu' and major < 22) or distro not in ('debian','ubuntu'):
        raise Error('Поддерживаются Debian 12+ и Ubuntu 22.04+')
    if not Path('/proc/sys/net/ipv6').exists():
        raise Error('IPv6 отсутствует в ядре (возможно ipv6.disable=1). Исправьте загрузку ОС и перезагрузите VPS.')
    if shutil.which('nft'):
        old = run('nft', 'list', 'table', 'inet', 'ygg_vless_access', check=False)
        if old:
            raise Error('Найден firewall отдельного ygg-vless. Его запрет остальных Yggdrasil-портов требует миграции; существующие правила не менялись. См. README.')


def setup_ygg(peers: list[str], backup: Path) -> str:
    arch = run('dpkg','--print-architecture').strip()
    if arch not in YGG_HASHES:
        raise Error('Поддерживаются amd64 и arm64')
    if not shutil.which('yggdrasil'):
        with tempfile.TemporaryDirectory(prefix='remna-ygg-') as directory:
            package = Path(directory)/'ygg.deb'
            download(f'https://github.com/yggdrasil-network/yggdrasil-go/releases/download/v{YGG_VERSION}/yggdrasil-{YGG_VERSION}-{arch}.deb', package, YGG_HASHES[arch])
            run('apt-get','install','-y',str(package),timeout=300)
    version = run('yggdrasil','-version')
    if not re.search(r'Build version: 0\.5\.',version):
        raise Error('Уже установлен Yggdrasil другой ветки. Автоматический downgrade не выполняется.')
    start = run('systemctl','show','yggdrasil.service','--property=ExecStart','--value')
    match = re.search(r'-useconffile(?:=|\s+)(?:"([^"\n]+)"|([^\s;}]+))', start)
    if not match:
        raise Error('Не удалось определить активный конфиг Yggdrasil из systemd')
    path = Path(match.group(1) or match.group(2))
    if not path.is_absolute() or '$' in str(path):
        raise Error('Неподдерживаемый путь конфига Yggdrasil')
    original = path.read_text() if path.exists() else ''
    if original:
        shutil.copy2(path, backup/'yggdrasil.conf')
        config = json.loads(run('yggdrasil','-useconffile',str(path),'-normaliseconf','-json'))
        # -normaliseconf clears AdminListen: restore only this original scalar.
        try:
            original_json=json.loads(original)
        except json.JSONDecodeError:
            original_json={}
            match = re.search(r'(?m)^[ \t]*(?:AdminListen|"AdminListen")[ \t]*:[ \t]*("(?:[^"\\]|\\.)*"|[^\r\n]*)',original)
            if match:
                value = match.group(1).strip()
                original_json['AdminListen']=json.loads(value) if value.startswith('"') else re.split(r'\s+(?://|#)',value,maxsplit=1)[0].rstrip(',').strip()
        if 'AdminListen' in original_json: config['AdminListen']=original_json['AdminListen']

    else:
        config = json.loads(run('yggdrasil','-genconf','-json'))
    if peers:
        if not all(p.startswith(('tls://','tcp://','quic://')) for p in peers):
            raise Error('Некорректный URI пира Yggdrasil')
        config['Peers'] = peers
    elif not config.get('Peers') and not any(config.get('InterfacePeers',{}).values()):
        config['Peers'] = PEERS
    if config.get('IfName') in (None,'','none','auto'):
        previous=run('yggdrasil','-useconffile',str(path),'-address').strip()
        interfaces=json.loads(run('ip','-j','-6','addr','show'))
        previous_iface=next((i['ifname'] for i in interfaces if any(a.get('local')==previous for a in i['addr_info'])),None)
        config['IfName'] = previous_iface or 'ygg-remna' 
    iface = config['IfName']
    if not re.fullmatch(r'[a-zA-Z0-9_.-]{1,15}', iface) or iface in ('lo','docker0'):
        raise Error('Неподдерживаемое имя интерфейса Yggdrasil')
    if config.get('AdminListen') in (None,'','none'):
        config['AdminListen'] = 'unix:///var/run/yggdrasil/yggdrasil.sock'
    # Own pre-start unit enables IPv6 only on the TUN and loopback. WAN/default stay untouched.
    user = run('systemctl','show','yggdrasil','--property=User','--value').strip() or 'root'
    write(BASE/'network.json',json.dumps({'interface':iface,'user':user}))
    write(Path('/etc/systemd/system/remna-ygg-network.service'),f'''[Unit]
Description=IPv6 on the Yggdrasil interface
After=systemd-sysctl.service
Before=yggdrasil.service
[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/usr/bin/python3 {APP}/main.py --network-only
[Install]
WantedBy=multi-user.target
''',0o644)
    write(Path('/etc/systemd/system/yggdrasil.service.d/remna-ygg.conf'),'''[Unit]
Requires=remna-ygg-network.service
After=remna-ygg-network.service
''',0o644)
    mode = path.stat().st_mode & 0o777 if path.exists() else 0o640
    write(path,json.dumps(config,indent=2)+'\n',mode)
    # Preserve package group ownership even though atomic replacement creates a new inode.
    import grp
    if any(g.gr_name == 'yggdrasil' for g in grp.getgrall()):
        shutil.chown(path,group='yggdrasil')
    run('systemctl','daemon-reload')
    run('systemctl','enable','remna-ygg-network','yggdrasil')
    run('systemctl','restart','remna-ygg-network')
    run('systemctl','restart','yggdrasil')
    address = run('yggdrasil','-useconffile',str(path),'-address').strip()
    if ipaddress.IPv6Address(address) not in ipaddress.IPv6Network('200::/8'):
        raise Error('Yggdrasil вернул неожиданный адрес')
    for _ in range(45):
        interfaces = json.loads(run('ip','-j','-6','addr','show'))
        assigned = any(a.get('local') == address for i in interfaces for a in i['addr_info'])
        status = run('yggdrasilctl','-endpoint='+config['AdminListen'],'-json','getPeers',timeout=5,check=False)
        if assigned and status and any(p.get('up') for p in json.loads(status).get('peers',[])):
            return address
        time.sleep(2)
    raise Error('Не появился адрес Yggdrasil или нет живых пиров; журнал: journalctl -u yggdrasil')


def network_only():
    config = json.loads((BASE/'network.json').read_text())
    iface = config['interface']
    device = Path('/sys/class/net')/iface
    if not device.exists():
        run('ip','tuntap','add','dev',iface,'mode','tun','user',config['user'])
    elif not (device/'tun_flags').exists():
        raise Error('Интерфейс Yggdrasil занят другим типом устройства')
    for interface in ('lo',iface):
        Path('/proc/sys/net/ipv6/conf',interface,'disable_ipv6').write_text('0\n')
    run('ip','link','set','dev',iface,'up')


def connect(address: str, port: int, banner: bool = False):
    with socket.socket(socket.AF_INET6,socket.SOCK_STREAM) as connection:
        connection.settimeout(3)
        connection.connect((address,port))
        if banner and not connection.recv(256).startswith(b'SSH-'):
            raise Error('Порт SSH обслуживается другим протоколом')


def ssh_config(address: str, port: int):
    managed=Path('/etc/systemd/system/remna-ygg-ssh.service').exists()
    if not managed:
        try:
            connect(address,port,banner=True)
            return
        except (OSError, Error):
            pass
    # Expand Include once, preserving authentication/Match. Bind only this IPv6.
    import glob
    lines,stack = [],[]
    in_match = False
    def expand(path: str):
        nonlocal in_match
        full = str(Path(path).resolve())
        if full in stack:
            raise Error('Циклический Include в sshd_config')
        stack.append(full)
        for raw in Path(full).read_text().splitlines():
            parts = shlex.split(raw,comments=True)
            if not parts:
                continue
            key = parts[0].lower()
            if key == 'include':
                for pattern in parts[1:]:
                    for child in sorted(glob.glob(pattern if pattern.startswith('/') else '/etc/ssh/'+pattern)):
                        expand(child)
            else:
                if key == 'match':
                    in_match = True
                if not in_match and key in ('port','listenaddress','addressfamily','pidfile'):
                    continue
                lines.append(raw)
        stack.pop()
    expand('/etc/ssh/sshd_config')
    Path('/run/sshd').mkdir(exist_ok=True)
    text = f'Port {port}\nAddressFamily inet6\nListenAddress {address}\nPidFile /run/remna-ygg-sshd.pid\n'+'\n'.join(lines)+'\n'
    write(BASE/'sshd_config',text)
    run('/usr/sbin/sshd','-t','-f',str(BASE/'sshd_config'))
    write(Path('/etc/systemd/system/remna-ygg-ssh.service'),f'''[Unit]
Description=SSH on Remnanode Yggdrasil address
After=yggdrasil.service remna-ygg-access.service
Requires=remna-ygg-access.service
StartLimitIntervalSec=0
[Service]
ExecStartPre=/usr/sbin/sshd -t -f {BASE}/sshd_config
ExecStart=/usr/sbin/sshd -D -e -f {BASE}/sshd_config
Restart=on-failure
RestartSec=5
[Install]
WantedBy=multi-user.target
''',0o644)
    run('systemctl','daemon-reload')
    run('systemctl','enable','remna-ygg-ssh')
    run('systemctl','restart','remna-ygg-ssh')
    for _ in range(10):
        try:
            connect(address,port,banner=True)
            return
        except OSError:
            time.sleep(1)
    raise Error('Отдельный SSH не запустился: journalctl -u remna-ygg-ssh')


def firewall(address: str, port: int, allowed: list[str], ssh_port: int, ssh_allowed: list[str], ready: bool = False):
    ufw = bool(shutil.which('ufw') and 'Status: active' in run('ufw','status'))
    if ufw and not re.search(r'(?m)^IPV6=yes\s*$',Path('/etc/default/ufw').read_text()):
        raise Error('У активного UFW отключён IPv6. Включите поддержку IPv6 до установки.')
    if not ufw and not ready:
        rules = json.loads(run('nft','-j','list','ruleset'))
        input_chains = [e['chain'] for e in rules['nftables'] if 'chain' in e and e['chain'].get('hook') == 'input' and e['chain']['table'] != TABLE]
        for chain in input_chains:
            nonempty = any('rule' in e and e['rule']['table'] == chain['table'] and e['rule']['chain'] == chain['name'] for e in rules['nftables'])
            if chain.get('policy') == 'drop' or nonempty:
                raise Error('Нестандартный INPUT firewall: настройте точечный доступ и повторите с --firewall-ready. Правила Docker не менялись.')
    if ufw:
        for service_port,clients in ((port,allowed),(ssh_port,ssh_allowed)):
            for client in clients:
                run('ufw','allow','in','from',client,'to',address,'port',str(service_port),'proto','tcp','comment','remna-ygg')
    sets = lambda values: ', '.join(values)
    text = f'''table inet {TABLE} {{
 set vpn_clients {{ type ipv6_addr; flags interval; auto-merge; elements = {{ {sets(allowed)} }} }}
 set ssh_clients {{ type ipv6_addr; flags interval; auto-merge; {('elements = { '+sets(ssh_allowed)+' };') if ssh_allowed else ''} }}
 chain vpn {{ ip6 saddr @vpn_clients accept; counter drop; }}
 chain ssh {{ ip6 saddr @ssh_clients accept; counter drop; }}
 chain input {{
  type filter hook input priority -10; policy accept;
  iifname "lo" accept
  ip6 daddr {address} tcp dport {port} jump vpn
  ip6 daddr {address} tcp dport {{ {', '.join(map(str,sorted({22,ssh_port})))} }} jump ssh
 }}
}}
'''
    write(BASE/'access.nft',text)
    helper = f'''#!/bin/bash
set -Eeuo pipefail
umask 077
scratch=$(mktemp)
trap 'rm -f -- "$scratch"' EXIT
if nft list table inet {TABLE} >/dev/null 2>&1; then
 printf 'delete table inet {TABLE}\\n' >"$scratch"
fi
cat {BASE}/access.nft >>"$scratch"
nft -c -f "$scratch"
nft -f "$scratch"
'''
    write(APP/'apply-access.sh',helper,0o700)
    write(Path('/etc/systemd/system/remna-ygg-access.service'),f'''[Unit]
Description=Remnanode Yggdrasil port access lists
After=nftables.service ufw.service firewalld.service
Before=remna-ygg-ssh.service
[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart={APP}/apply-access.sh
ExecReload={APP}/apply-access.sh
ExecStop=-/usr/sbin/nft delete table inet {TABLE}
[Install]
WantedBy=multi-user.target
''',0o644)
    run('systemctl','daemon-reload')
    run('systemctl','enable','remna-ygg-access')
    run('systemctl','reload-or-restart','remna-ygg-access')
    if not ssh_allowed and Path('/etc/systemd/system/remna-ygg-ssh.service').exists():
        run('systemctl','disable','--now','remna-ygg-ssh')
