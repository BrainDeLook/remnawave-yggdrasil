#!/usr/bin/env python3
"""Native Remnawave host over Yggdrasil; existing Xray profile stays untouched."""
from __future__ import annotations
import argparse
import copy
import datetime
import getpass
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import select
import shutil
import socket
import subprocess
import sys
import termios
from uuid import UUID

from api import API, Error, Host, Node, eligible, host_payload, obj, string, upsert
import node as local
from probe import export, verify
VERSION='0.1.0'

COLOR=sys.stdout.isatty() and os.environ.get('TERM')!='dumb' and 'NO_COLOR' not in os.environ

def say(message: str, color='36'):
    print(('\033['+color+'m'+message+'\033[0m') if COLOR else message,flush=True)

def prompt(label: str, default: str='') -> str:
    say(label+(f' [{default}]' if default else '')+':','33')
    return input('  ').strip() or default

def choose(items: list, key: str | None, saved: str | None, yes: bool, label: str):
    if not items:
        raise Error('Не найдено подходящих вариантов: '+label)
    if key:
        selected=next((v for v in items if v.uuid==key),None)
        if selected is None: raise Error('Не найден выбранный UUID: '+label)
        return selected
    if saved:
        found=next((v for v in items if v.uuid==saved),None)
        if found: return found
    if len(items)==1:
        say(label+': '+items[0].name)
        return items[0]
    if yes:
        raise Error('Несколько вариантов. Укажите UUID параметром: '+label)
    say(label)
    for i,item in enumerate(items,1): say(f'  {i}. {item.name} ({item.uuid})')
    while True:
        value=prompt('Номер')
        if value.isdigit() and 1<=int(value)<=len(items): return items[int(value)-1]

def access(values: list[str], old: list[str], yes: bool, kind: str, role: str='any') -> list[str]:
    if values: return local.networks(values,role=role)
    if yes:
        if not old: raise Error('Укажите --allow-ip или --ssh-allow-ip для '+kind)
        return local.networks(old,role=role)
    while True:
        text=prompt(kind,' '.join(old))
        try: return local.networks(text.split(),role=role)
        except Error as exc: say(str(exc),'31')

def result():
    state=json.loads((local.BASE/'state.json').read_text())
    say('\n  ГОТОВО · '+state['name'],'1;32')
    say('  Host в Remnawave: '+state['host_uuid'])
    say(f"  Адрес ноды: {state['address']}   VLESS-порт: {state['port']}")
    say('  SNI Reality: '+state['sni'])
    say('  VLESS: '+' '.join(state['allowed']))
    if state['ssh']:
        say('  SSH: '+' '.join(state['ssh_allowed']))
        print(f"ssh -6 -p {state['ssh_port']} root@{state['address']}")
        say('  Используйте существующего SSH-пользователя и его ключ/пароль.')
    else: say('  SSH через Yggdrasil отключён.')
    say('  Проверка: '+state['verification'],'32')
    for title,filename in [('ЛИЧНАЯ ССЫЛКА','client.txt'),('JSON ТАХИОНА','client-tachyon.json')]:
        file=local.BASE/filename
        if file.exists():
            say('\n  ┌─ '+title+' ─────────────────────────────────','35')
            print(file.read_text().strip())
            say('  └────────────────────────────────────────────────','35')
    say('\n  Обновите личную подписку в клиенте: новый Host появится у пользователей,\n  которым доступен выбранный inbound и разрешения исходного Host.\n  Тахиону может требоваться bind_interface: yggdrasil. Общего UUID здесь нет.')


def main():
    parser=argparse.ArgumentParser(description='Remnawave + Yggdrasil: native VLESS Reality, без второго proxy core')
    parser.add_argument('--version',action='version',version=VERSION)
    parser.add_argument('--panel',help='HTTPS URL панели')
    parser.add_argument('--token-file',type=Path,help='файл с API-токеном; токен в argv не принимается')
    parser.add_argument('--node',help='UUID именно этого VPS в панели')
    parser.add_argument('--source-host',help='UUID действующего VLESS TCP Reality Host')
    parser.add_argument('--name',help='название нового Host')
    parser.add_argument('--allow-ip',action='append',default=[],help='IPv6 клиента или LAN /64; повторяемый')
    parser.add_argument('--ssh-allow-ip',action='append',default=[])
    group=parser.add_mutually_exclusive_group(); group.add_argument('--ssh',action='store_true'); group.add_argument('--no-ssh',action='store_true')
    parser.add_argument('--ssh-port',type=int,default=None)
    parser.add_argument('--user-uuid',help='UUID пользователя панели для полной HTTPS-проверки и личной ссылки')
    parser.add_argument('--peer',action='append',default=[])
    parser.add_argument('--yes',action='store_true')
    parser.add_argument('--check',action='store_true',help='только API/план, без изменений')
    parser.add_argument('--result',action='store_true')
    parser.add_argument('--network-only',action='store_true',help=argparse.SUPPRESS)
    parser.add_argument('--firewall-ready',action='store_true')
    args=parser.parse_args()
    if args.network_only: local.network_only(); return
    if args.result: result(); return
    local.preflight()
    old=json.loads((local.BASE/'state.json').read_text()) if (local.BASE/'state.json').exists() else {}
    if sys.stdin.isatty(): termios.tcflush(sys.stdin.fileno(),termios.TCIFLUSH)
    say('\n  ╔════════════════════════════════════════════════════╗\n  ║       Remnawave + Yggdrasil · '+VERSION+'              ║\n  ╚════════════════════════════════════════════════════╝','1;36')
    panel=args.panel or old.get('panel')
    if not panel and not args.yes: panel=prompt('URL панели, например https://panel.example.com')
    if not panel: raise Error('Укажите --panel')
    if args.token_file:
        if args.token_file.stat().st_mode & 0o077: raise Error('Файл токена должен быть закрыт: chmod 600')
        token=args.token_file.read_text().strip()
    else:
        token=os.environ.pop('REMNAWAVE_API_TOKEN','')
        if not token and not args.yes: token=getpass.getpass('  API-токен (скрытый ввод, не сохраняется): ').strip()
    api=API(panel,token)
    say('[1/5] Проверка панели и существующей ноды…')
    selected=choose(api.nodes(),args.node,old.get('node_uuid'),args.yes,'Нода ЭТОГО VPS')
    hosts=api.hosts()
    source=choose([h for h in hosts if eligible(selected,h)],args.source_host,old.get('source_host_uuid'),args.yes,'Исходный VLESS Reality Host')
    raw=api.inbound(selected,source)
    profile_before=json.dumps(raw,sort_keys=True)
    say(f'  Выбрана нода: {selected.name}; адрес API: {selected.address}\n  Используется существующий порт {source.port}, SNI и пользователи панели.')
    name=args.name or old.get('name') or source.name+' · Yggdrasil'
    if not args.yes and not args.name: name=prompt('Название подключения',name)
    ssh=not args.no_ssh and (args.ssh or old.get('ssh',False))
    if not args.yes:
        answer=prompt('Нужен SSH через Yggdrasil? да/нет','да' if ssh else 'нет')
        while answer.lower() not in ('да','нет','yes','no','y','n'):
            answer=prompt('Введите да или нет')
        ssh=answer.lower() in ('да','yes','y')
    say('  На роутере IPv6 200:… копируйте БЕЗ /7 — для Тахиона.\n  IPv6-PD 300:…/64 копируйте С /64 — для ПК и LAN.\n  Примеры: 200:1111:2222:3333:4444:5555:6666:7777 и 300:1111:2222:3333::/64.\n  Можно несколько значений через пробел; адрес VPS в списки не вводите.')
    if args.allow_ip or args.yes:
        allowed=local.networks(args.allow_ip or old.get('allowed',[]),pair=True)
    else:
        previous=old.get('allowed',[])
        routers=access([],[v for v in previous if v.startswith('2')],False,'IPv6 роутеров для VLESS (без /7)','router')
        lans=access([],[v for v in previous if v.startswith('3') and v.endswith('/64')],False,'Подсети LAN для VLESS (с /64)','lan')
        allowed=routers+lans+[v for v in previous if v.startswith('3') and not v.endswith('/64')]
    ssh_saved=args.ssh_allow_ip or old.get('ssh_allowed',[])
    ssh_allowed=access(args.ssh_allow_ip,ssh_saved,args.yes,'SSH · отдельные разрешённые IP/подсети') if ssh else []
    ssh_port=args.ssh_port or old.get('ssh_port',22)
    if not 1<=ssh_port<=65535 or source.port in (22,ssh_port) or ssh_port==2222:
        raise Error('Порт VLESS и SSH должны различаться; 2222 зарезервирован для API Remnanode')
    user_uuid=args.user_uuid
    if not args.yes and not user_uuid:
        user_uuid=prompt('UUID пользователя панели для проверки/личной ссылки (Enter — пропустить)') or None
    user=obj(api.request('GET','/users/'+str(UUID(user_uuid)))) if user_uuid else None
    # Plan and all API validation happen before package installation or firewall changes.
    host_payload(selected,source,raw,'200::1',name)
    if args.check:
        say('  Проверка API успешна. План: Yggdrasil + точечный firewall + дополнительный Host. Профиль Xray не меняется.','32')
        return
    local.BASE.mkdir(mode=0o700,parents=True,exist_ok=True)
    local.BASE.chmod(0o700)
    import fcntl
    with open(local.BASE/'install.lock','w') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        stamp=datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%SZ')
        backup=local.BASE/'backups'/stamp; backup.mkdir(parents=True,mode=0o700)
        for filename in ('state.json','access.nft','network.json','sshd_config'):
            if (local.BASE/filename).exists(): shutil.copy2(local.BASE/filename,backup/filename)
        local.APP.mkdir(parents=True,exist_ok=True)
        for filename in ('api.py','node.py','probe.py','main.py'):
            origin=Path(__file__).parent/filename
            if origin.resolve()!=(local.APP/filename).resolve(): shutil.copy2(origin,local.APP/filename)
            (local.APP/filename).chmod(0o700)
        say('[2/5] Yggdrasil: пакет, интерфейс, сохранение идентичности…')
        os.environ['DEBIAN_FRONTEND']='noninteractive'
        local.run('apt-get','update',timeout=300)
        local.run('apt-get','install','-y','ca-certificates','curl','openssl','iproute2','nftables','kmod',timeout=300)
        if ssh: local.run('apt-get','install','-y','openssh-server',timeout=300)
        local.run('modprobe','tun',check=False)
        address=local.setup_ygg(args.peer,backup)
        say('[3/5] Проверка IPv6-слушателя существующего Xray…')
        try: local.connect(address,source.port)
        except OSError:
            raise Error('Xray не принимает IPv6 на Yggdrasil-адресе. Профиль не менялся. Проверьте listen, sockopt.v6only и Docker network_mode: host; --check проверяет только API.') from None
        payload=host_payload(selected,source,raw,address,name)
        say('[4/5] Раздельные разрешения VLESS/SSH. Правила Docker сохраняются…')
        local.firewall(address,source.port,allowed,ssh_port,ssh_allowed,args.firewall_ready)
        if ssh: local.ssh_config(address,ssh_port)
        verification='IPv6 TCP-слушатель доступен; проверка с пользователем не запрошена'
        link=None; out=None
        if user:
            say('  Проверка реального VLESS Reality и HTTPS с выбранным пользователем…')
            link,out=export(raw,payload,user)
            verify(out)
            verification='VLESS Reality → HTTPS: HTTP 204 (локальный тест, не проверка с вашего роутера)'
        if profile_before!=json.dumps(api.inbound(selected,source),sort_keys=True):
            raise Error('Профиль изменился во время установки. Повторите запуск; Host пока не записан.')
        say('[5/5] Создание/обновление только нашего Host в панели…')
        endpoint=upsert(api,payload,api.hosts(),old.get('host_uuid'))
        state={'version':VERSION,'panel':panel,'node_uuid':selected.uuid,'source_host_uuid':source.uuid,
               'host_uuid':endpoint.uuid,'name':name,'address':address,'port':source.port,'sni':payload['sni'],
               'allowed':allowed,'ssh':ssh,'ssh_port':ssh_port,'ssh_allowed':ssh_allowed if ssh else ssh_saved,'verification':verification}
        local.write(local.BASE/'state.json',json.dumps(state,ensure_ascii=False,indent=2)+'\n')
        for filename in ('client.txt','client-tachyon.json'):
            (local.BASE/filename).unlink(missing_ok=True)
        if link and out:
            local.write(local.BASE/'client.txt',link+'\n')
            local.write(local.BASE/'client-tachyon.json',json.dumps(dict(out,bind_interface='yggdrasil'),ensure_ascii=False,indent=2)+'\n')
        local.write(Path('/usr/local/sbin/remnawave-yggdrasil'),f'#!/bin/sh\nexec /usr/bin/python3 {local.APP}/main.py "$@"\n',0o700)
    result()

if __name__=='__main__':
    os.umask(0o077)
    os.environ['LC_ALL']='C'
    try: main()
    except (Error,OSError,ValueError,KeyError,subprocess.SubprocessError) as exc:
        say('\n  Ошибка: '+str(exc),'31')
        say('  API-токен не сохранён. Профиль Xray и существующие Host не редактируются.\n  Локальные изменения могли уже примениться; исправьте причину и повторите запуск.\n  Журналы: journalctl -u yggdrasil -u remna-ygg-network -u remna-ygg-access -u remna-ygg-ssh')
        sys.exit(1)
