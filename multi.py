#!/usr/bin/env python3
"""Interactive node extensions managed as ordinary Remnawave profile inbounds."""
from __future__ import annotations
import argparse
import datetime
import getpass
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import termios
import time
from api import API, AccessRequired, Error, obj
from converter import Profile, convert, LABELS
from main import say, prompt, choose, access
import node as local
import certificates
import provision

VERSION='0.2.0'
FILES=('api.py','main.py','node.py','probe.py','multi.py','converter.py','provision.py','certificates.py')


def result():
    state=json.loads((local.BASE/'multi-state.json').read_text())
    say('\n  ╔═ ГОТОВО · '+state['name']+' ═╗','1;32')
    say('  Профиль Remnawave: '+state['profile'])
    for host in state['hosts']:
        say(f"  {LABELS[host['protocol']]}\n    inbound: {host['tag']}\n    Host: {host['name']} · {host['address']}:{host['port']}")
    if state.get('address'): say('  IPv6 Yggdrasil ноды: '+state['address'])
    if state.get('ssh'): say('  SSH: ssh -6 -p '+str(state['ssh_port'])+' root@'+state['address'])
    say('  Откройте Remnawave → Внутренние сквады: новые inbound можно включать отдельно.\n  Host → ограничения внутренних сквадов управляют показом каждого Host в подписке.\n  Если сквады выбраны, на новом Host разрешён показ только им.\n  Если сквады не выбраны, новый Host ВЫКЛЮЧЕН. В панели выберите ему\n  разрешённые сквады, разрешите inbound и включите Host.\n  Обновите обычную личную подписку в клиенте. Общий UUID не создаётся.','32')
    say('  Тахион: подписка не передаёт bind_interface. На некоторых версиях требуется\n  привязка к интерфейсу Yggdrasil на роутере. Это ограничение клиента, не панели.','33')
    say('  Проверены API-настройки и локальные слушатели. Проверка реального VPN\n  выполняется вашим клиентом после обновления подписки.')


def main():
    parser=argparse.ArgumentParser(description='Remnawave: дополнительные протоколы в существующем профиле и сквадах')
    parser.add_argument('--version',action='version',version=VERSION)
    parser.add_argument('--panel'); parser.add_argument('--token-file',type=Path); parser.add_argument('--access-file',type=Path)
    parser.add_argument('--node'); parser.add_argument('--source-host'); parser.add_argument('--name')
    parser.add_argument('--protocol',action='append',choices=LABELS,default=[])
    parser.add_argument('--squad',action='append',default=[],help='UUID сквада для выбранных протоколов; повторяемый')
    parser.add_argument('--allow-ip',action='append',default=[])
    parser.add_argument('--ssh-allow-ip',action='append',default=[])
    ssh=parser.add_mutually_exclusive_group(); ssh.add_argument('--ssh',action='store_true'); ssh.add_argument('--no-ssh',action='store_true')
    parser.add_argument('--ssh-port',type=int,default=22)
    parser.add_argument('--ygg-port',type=int,default=1488)
    parser.add_argument('--cert',type=Path); parser.add_argument('--key',type=Path)
    parser.add_argument('--peer',action='append',default=[])
    parser.add_argument('--firewall-ready',action='store_true')
    parser.add_argument('--yes',action='store_true'); parser.add_argument('--check',action='store_true')
    parser.add_argument('--result',action='store_true'); parser.add_argument('--network-only',action='store_true',help=argparse.SUPPRESS)
    parser.add_argument('--sync-certs',action='store_true',help=argparse.SUPPRESS)
    args=parser.parse_args()
    if args.network_only: local.network_only(); return
    if args.sync_certs: certificates.sync(); return
    if args.result: result(); return
    local.preflight()
    if sys.stdin.isatty(): termios.tcflush(sys.stdin.fileno(),termios.TCIFLUSH)
    say('\n  ╔══════════════════════════════════════════════════════╗\n  ║       Remnawave · дополнительные протоколы 0.2.0      ║\n  ╚══════════════════════════════════════════════════════╝','1;36')
    say('  Запускайте на VPS уже работающей ноды.\n  Базовый VLESS остаётся. Выберите дополнения: мы подготовим зависимости,\n  добавим inbound в профиль, Host и выбранные сквады через API.\n  Не нужен VPN Username и не нужно вручную вставлять JSON в панель.')
    old=json.loads((local.BASE/'multi-state.json').read_text()) if (local.BASE/'multi-state.json').exists() else {}
    panel=args.panel or old.get('panel')
    if not panel and not args.yes:
        say('\n  Шаг 1 · Доступ к панели\n  URL — адрес страницы входа в Remnawave: https://panel.example.com.\n  Здесь НЕ адрес VPS ноды, не ссылка подписки и не секретная ссылка eGames.')
        panel=prompt('URL панели')
    if not panel: raise Error('Укажите --panel')
    token=os.environ.pop('REMNAWAVE_API_TOKEN','')
    if args.token_file:
        if args.token_file.stat().st_mode & 0o077: raise Error('Закройте файл токена: chmod 600')
        token=args.token_file.read_text().strip()
    if not token and not args.yes:
        say('  Токен: Настройки Remnawave → Токены API → отдельный временный токен.\n  Нужны чтение и изменение Profiles, Nodes, Hosts, Internal Squads.\n  Для первого запуска можно полный доступ. Токен не сохраняется.')
        token=getpass.getpass('  API-токен (ввод скрыт): ').strip()
    api=API(panel,token)
    secret=os.environ.pop('REMNAWAVE_PANEL_ACCESS','')
    if args.access_file:
        if args.access_file.stat().st_mode & 0o077: raise Error('Закройте файл доступа: chmod 600')
        secret=args.access_file.read_text().strip()
    if secret: api.set_access(secret)
    try: nodes=api.nodes()
    except AccessRequired:
        if args.yes or secret: raise
        say('  Панель вернула HTML. Для cookie-защиты eGames нужна полная секретная\n  ссылка из результата установки/меню eGames:\n  https://panel.example.com/auth/login?СЕКРЕТНОЕ_ИМЯ=СЕКРЕТНОЕ_ЗНАЧЕНИЕ\n  После входа браузер убирает секрет из адресной строки. Возьмите исходную\n  ссылку eGames. Не отправляйте её в чат. Можно Cookie name=value.','33')
        api.set_access(getpass.getpass('  Секретная ссылка или Cookie (скрыто): '))
        nodes=api.nodes()
    say('\n  Шаг 2 · Выбор ноды\n  Выберите именно ЭТУ VPS, на которой сейчас запущен мастер.\n  Нода должна быть подключена к панели; обычное VPN-подключение уже работает.')
    selected=choose(nodes,args.node,old.get('node_uuid'),args.yes,'Нода ЭТОГО VPS')
    hosts=api.hosts()
    available=[h for h in hosts if h.profile==selected.profile and h.inbound in {i.uuid for i in selected.inbounds}
               and (not h.nodes or selected.uuid in h.nodes) and not h.raw.get('isDisabled') and not h.raw.get('isHidden')
               and not any(t.startswith('REMNA_MULTI_') or t=='YGGDRASIL_NATIVE' for t in h.raw.get('tags',[]))]
    profile=obj(api.request('GET','/config-profiles/'+selected.profile))
    typed=Profile.parse(profile['config'])
    base_ids={i.uuid for i in selected.inbounds if i.tag in typed.bases}
    source=choose([h for h in available if h.inbound in base_ids],args.source_host,old.get('source_host_uuid'),args.yes,'Исходный рабочий VLESS TCP Reality Host')
    base=next(i.tag for i in selected.inbounds if i.uuid==source.inbound)
    say(f'  Нода: {selected.name}; адрес API: {selected.address}; профиль: {profile["name"]}\n  Исходный Host: {source.name}; inbound: {base}')
    say('\n  Шаг 3 · Дополнительные протоколы\n  Базовый TCP Reality уже работает и сохраняется.\n  1 · Hysteria2: UDP, нужен действующий сертификат домена\n  2 · VLESS gRPC Reality: TCP 8443\n  3 · VLESS XHTTP Reality: TCP 4443\n  4 · VLESS Reality через Yggdrasil: отдельный TCP 1488, доступ через Yggdrasil\n  Введите номера через пробел, например 1 4. Каждый вариант необязателен.')
    protocols=args.protocol
    if not protocols and not args.yes:
        keys=list(LABELS)
        while not protocols:
            answer=prompt('Нужные дополнения','4').split()
            if answer and all(n in ('1','2','3','4') for n in answer): protocols=list(dict.fromkeys(keys[int(n)-1] for n in answer))
            else: say('  Нужны номера 1–4 через пробел. Например: 4','31')
    config,mapping=convert(typed,base,protocols,{'ygg':args.ygg_port})
    raw_by_tag={i['tag']:i for i in config['inbounds']}
    name=args.name or old.get('name') or selected.name
    if not args.yes and not args.name: name=prompt('Название сервера (к нему добавятся названия протоколов)',name)
    if not 1<=len(name)<=60 or any(c in name for c in '\r\n'): raise Error('Название должно содержать от 1 до 60 символов')
    all_squads=provision.squads(api)
    known={s['uuid'] for s in all_squads}
    if any(v not in known for v in args.squad): raise Error('Не найден указанный UUID сквада')
    grants={p:list(args.squad) for p in protocols}
    say('\n  Шаг 4 · Кому доступны новые подключения\n  Внутренний сквад — группа VPN-пользователей в Remnawave.\n  Мастер добавит inbound в выбранные сквады, сохранив прежние разрешения.\n  На НОВЫХ Host дополнительно разрешит показ только этим сквадам.\n  Ничего не выбрали — новый Host создаётся ВЫКЛЮЧЕННЫМ. Потом в панели\n  назначьте сквады для inbound/Host и включите Host.\n  На уже созданных Host ручные ограничения панели сохраняются.')
    for p in protocols:
        if args.yes or args.squad: continue
        say('  '+LABELS[p])
        for index,squad in enumerate(all_squads,1): say(f'    {index}. {squad["name"]}')
        while True:
            answer=prompt('Номера сквадов через пробел; Enter — настрою в панели').split()
            if not answer: break
            if all(v.isdigit() and 1<=int(v)<=len(all_squads) for v in answer):
                grants[p]=list(dict.fromkeys(all_squads[int(v)-1]['uuid'] for v in answer)); break
            say('  Выберите номера из списка или нажмите Enter.','31')
    allowed=[]; ssh_allowed=[]; ssh_enabled=False
    if 'ygg' in protocols:
        say('\n  Шаг 5 · Yggdrasil: сеть и SSH\n  Сквад разрешает VPN-учётку, а эти адреса — устройства, которым VPS\n  разрешает подключаться по сети. Для доступа нужны оба разрешения.\n  На OpenWrt: Сеть → Интерфейсы → Yggdrasil.\n  IPv6 200:…/7 → копируйте БЕЗ /7 (сам роутер и Тахион).\n  IPv6-PD 300:…/64 → копируйте С /64 (вся LAN, включая меняющиеся IP ПК).\n  Отдельный IP ПК из этой LAN не требуется. Адрес VPS сюда НЕ вводите.\n  Можно несколько адресов роутеров и несколько подсетей друзей через пробел.')
        ssh_enabled=args.ssh or (old.get('ssh',False) and not args.no_ssh)
        if not args.yes and not args.ssh and not args.no_ssh:
            answer=prompt('Открыть также SSH через Yggdrasil? да/нет','да' if ssh_enabled else 'нет').lower()
            while answer not in ('да','нет','yes','no'): answer=prompt('Введите да или нет').lower()
            ssh_enabled=answer in ('да','yes')
        previous=old.get('allowed',[])
        if args.allow_ip or args.yes: allowed=local.networks(args.allow_ip or previous,pair=True)
        else:
            allowed=access([],[v for v in previous if v.startswith('2')],False,'IPv6 роутеров: пример 200:1111:2222:3333:4444:5555:6666:7777','router')
            allowed+=access([],[v for v in previous if v.startswith('3')],False,'Подсети LAN: пример 300:1111:2222:3333::/64','lan')
        if ssh_enabled:
            say('  Это ОТДЕЛЬНЫЙ список для управления сервером по SSH.\n  Доступ к VLESS не открывает SSH. Если SSH нужен с тех же устройств,\n  можно принять показанный список Enter. Ключ/пароль SSH по-прежнему нужен.')
            ssh_allowed=access(args.ssh_allow_ip,old.get('ssh_allowed') or allowed,args.yes,'Разрешить SSH этим IP/подсетям')
    ports=[raw_by_tag[tag]['port'] for tag in mapping.values()]
    if ssh_enabled and args.ssh_port in [raw_by_tag[tag]['port'] for p,tag in mapping.items() if p!='hy2']:
        raise Error('SSH-порт совпадает с TCP-портом выбранного дополнения')
    if 'ygg' in protocols and (raw_by_tag[mapping['ygg']]['port'] in (22,2222,args.ssh_port) or args.ssh_port==2222):
        raise Error('Yggdrasil VLESS, SSH и API Remnanode (2222) должны иметь разные порты')
    if not 1<=args.ssh_port<=65535: raise Error('Некорректный SSH-порт')
    docker=json.loads(local.run('docker','inspect','remnanode'))[0]
    if not docker['State']['Running'] or docker['HostConfig']['NetworkMode']!='host':
        raise Error('Нужна работающая remnanode с network_mode: host. Docker Compose автоматически не переписывается.')
    old_tags={i['tag'] for i in typed.config['inbounds']}
    for protocol,tag in mapping.items():
        if tag in old_tags: continue
        port=raw_by_tag[tag]['port']
        listeners=local.run('ss','-H','-lnu' if protocol=='hy2' else '-lnt')
        if any(len(parts:=line.split())>=4 and parts[3].rpartition(':')[2]==str(port) for line in listeners.splitlines()):
            raise Error(f'Порт {port} уже слушает другой сервис. Настройки ещё не менялись; выберите другой порт/профиль.')
    cert=args.cert; key=args.key
    if 'hy2' in protocols:
        if not any(m['Destination']=='/dev/shm' and m['Source']=='/dev/shm' for m in docker['Mounts']):
            raise Error('Для Hysteria2 требуется общий /dev/shm:/dev/shm в Compose ноды, как у eGames')
        if docker['Config'].get('User') not in ('','0','root','0:0'):
            raise Error('Hysteria2: эта версия ожидает root внутри контейнера для чтения ключа 600')
        candidates=[p for p in Path('/etc/letsencrypt/live').glob('*/fullchain.pem') if p.parent.name==source.address]
        cert=cert or (candidates[0] if len(candidates)==1 else None)
        if not cert and not args.yes:
            say('  Hysteria2 нужен TLS-сертификат домена '+source.address+'.\n  Пример пути: /etc/letsencrypt/live/ваш-домен/fullchain.pem')
            cert=Path(prompt('Путь к полному сертификату'))
        if not cert: raise Error('Укажите --cert и --key для Hysteria2')
        key=key or cert.parent/'privkey.pem'
        if not cert.is_file() or not key.is_file(): raise Error('Не найдены сертификат или ключ Hysteria2')
        local.run('openssl','x509','-in',str(cert),'-noout','-checkhost',source.address)
    say('\n  План применения','1;35')
    say('  Нода: '+selected.name+' · '+selected.address+'\n  Профиль: '+profile['name']+' (если общий — новые inbound не активируются на других нодах)')
    for p,tag in mapping.items():
        names=[s['name'] for s in all_squads if s['uuid'] in grants[p]]
        say(f'  {LABELS[p]}: {tag}, порт {raw_by_tag[tag]["port"]}; сквады: '+(', '.join(names) or 'не добавлять доступ; настройки существующего Host сохранить'))
    say('  Старые inbound, Host и разрешения сохраняются. Применение профиля\n  перезагружает Xray штатным механизмом панели; возможен короткий разрыв.\n  Ничего не удаляется при снятии протокола в этом мастере.','33')
    if args.check: say('  Проверка плана успешна; изменений нет.','32'); return
    if not args.yes and prompt('Применить этот план? да/нет','нет').lower() not in ('да','yes'):
        say('  Отменено. Настройки не менялись.'); return
    local.BASE.mkdir(parents=True,exist_ok=True,mode=0o700); local.BASE.chmod(0o700)
    import fcntl
    with (local.BASE/'install.lock').open('w') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        stamp=datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%SZ')
        backup=local.BASE/'backups'/stamp; backup.mkdir(parents=True,mode=0o700)
        if (local.BASE/'multi-state.json').exists(): shutil.copy2(local.BASE/'multi-state.json',backup/'multi-state.json')
        local.APP.mkdir(parents=True,exist_ok=True)
        for filename in FILES:
            origin=Path(__file__).parent/filename
            if origin.resolve()!=(local.APP/filename).resolve(): shutil.copy2(origin,local.APP/filename)
            (local.APP/filename).chmod(0o700)
        say('\n  [1/4] Подготовка зависимостей…')
        os.environ['DEBIAN_FRONTEND']='noninteractive'
        local.run('apt-get','update',timeout=300)
        local.run('apt-get','install','-y','python3','curl','ca-certificates','openssl','iproute2','nftables','kmod',timeout=300)
        address=old.get('address','')
        if 'ygg' in protocols:
            say('  [2/4] Yggdrasil и списки сетевого доступа…')
            local.run('modprobe','tun',check=False)
            address=local.setup_ygg(args.peer,backup)
            legacy=json.loads((local.BASE/'state.json').read_text()) if (local.BASE/'state.json').exists() else {}
            extra=tuple({legacy['port']} if legacy.get('address')==address else set())
            local.firewall(address,raw_by_tag[mapping['ygg']]['port'],allowed,args.ssh_port,ssh_allowed,
                           args.firewall_ready,exclusive=True,extra_vpn_ports=extra)
            if ssh_enabled:
                local.run('apt-get','install','-y','openssh-server',timeout=300)
                local.ssh_config(address,args.ssh_port)
        else: say('  [2/4] Yggdrasil не выбран; его настройки не меняются.')
        ufw=bool(shutil.which('ufw') and 'Status: active' in local.run('ufw','status'))
        for p,tag in mapping.items():
            if p!='ygg' and ufw: local.run('ufw','allow',str(raw_by_tag[tag]['port'])+('/udp' if p=='hy2' else '/tcp'))
        if 'hy2' in protocols: certificates.setup(cert,key)
        say('  [3/4] Профиль, активные inbound и внутренние сквады…')
        # Root-only journal is useful if an API call times out after being applied.
        local.write(backup/'plan.json',json.dumps({'node':selected.uuid,'mapping':mapping,'grants':grants}))
        created=provision.apply(api,selected,profile['config'],config,mapping,source,address,name,grants,backup)
        state=dict(panel=panel,name=name,node_uuid=selected.uuid,profile=selected.profile,source_host_uuid=source.uuid,
                   address=address,allowed=allowed,ssh=ssh_enabled,ssh_allowed=ssh_allowed,ssh_port=args.ssh_port,hosts=created)
        local.write(local.BASE/'multi-state.json',json.dumps(state,ensure_ascii=False,indent=2))
        local.write(Path('/usr/local/sbin/remnawave-yggdrasil'),f'#!/bin/sh\nexec /usr/bin/python3 {local.APP}/multi.py "$@"\n',0o700)
        say('  [4/4] Ожидание TCP-слушателей…')
        for p,tag in mapping.items():
            if p=='hy2': continue
            target=address if p=='ygg' else '127.0.0.1'
            for attempt in range(30):
                try:
                    with socket.create_connection((target,raw_by_tag[tag]['port']),timeout=2): break
                except OSError:
                    if attempt==29: raise Error('Не появился слушатель '+tag+'. Проверьте журнал ноды и профиль; уже созданные настройки сохранены.') from None
                    time.sleep(2)
    result()

if __name__=='__main__':
    os.umask(0o077); os.environ['LC_ALL']='C'
    try: main()
    except (Error,OSError,ValueError,KeyError,subprocess.SubprocessError) as exc:
        say('\n  Ошибка: '+str(exc),'31')
        say('  Токен и секретная ссылка не сохраняются.\n  Если ошибка после подтверждения плана, часть изменений могла примениться.\n  Повторный запуск сохраняет существующие inbound/Host.\n  Резервные копии: /etc/remnawave-yggdrasil/backups\n  Журналы: journalctl -u yggdrasil -u remna-ygg-access -u remna-hy2-certs; docker logs --tail 80 remnanode')
        sys.exit(1)
