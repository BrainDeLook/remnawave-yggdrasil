#!/usr/bin/env python3
"""Interactive node extensions managed as ordinary Remnawave profile inbounds."""
from __future__ import annotations
import argparse
import datetime
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import termios
import time
import ui
from api import API, AccessRequired, Error, obj, seq, string, uid
from converter import Profile, convert, LABELS, HOST_SUFFIXES
from ui import say, prompt, choose, access, section, field, option, panel as frame, secret as secret_prompt, confirm
import node as local
import certificates
import provision

VERSION='0.3.4'
FILES=('api.py','main.py','node.py','probe.py','multi.py','converter.py','provision.py','certificates.py','ui.py','screen.py')


def result(*, after_install=False):
    ui.page('Результат установки' if after_install else 'Сохранённые подключения')
    path=local.BASE/'multi-state.json'
    if not path.exists(): raise Error('Сохранённых подключений пока нет. Сначала выберите установку дополнительных протоколов.')
    state=json.loads(path.read_text())
    if after_install:
        frame('УСТАНОВКА ЗАВЕРШЕНА',[('Настройки применены. Обновите подписку в своём VPN-клиенте.','success')],'success')
    frame('НОДА',[(state['name'],'value'),
          ('Панель: '+state['panel'],'example'),
          ('Профиль: '+state.get('profile_name',state['profile']),'text'),
          ('Снимок: '+state['saved_at'] if state.get('saved_at') else 'Снимок последней установки; время не сохранено.','muted'),
          ('Это сохранённые настройки. Последующие изменения в панели здесь не отражаются.','muted')])
    for host in state['hosts']:
        endpoint=('['+host['address']+']') if ':' in host['address'] else host['address']
        frame(host['name'],[('Протокол: '+LABELS[host['protocol']],'text'),
              ('Сервер: '+endpoint+':'+str(host['port']),'example'),
              ('Inbound: '+host['tag'],'muted'),
              ('Выключен: назначьте сквады в панели и включите Host.' if host.get('disabled') else 'Включён: показ в подписке зависит от разрешений сквадов.',
               'warning' if host.get('disabled') else 'text')])
    frame('ПОДПИСКА',[
          ('Используйте личную подписку VPN-пользователя из Remnawave.','text'),
          ('Обновите её в клиенте и выберите одно из подключений выше.','text'),
          ('Общей VPN-ссылки и UUID нет: у каждого пользователя свои реквизиты.','muted')])
    if state.get('address'):
        network=[('IPv6 этой VPS: '+state['address'],'example'),
                 ('VPN: по подписке, без списка IP.' if state.get('access_mode')=='subscription' else
                  'VPN: по подписке и списку разрешённых IP.','text')]
        if state.get('access_mode')!='subscription':
            network.extend((value,'value') for value in state.get('allowed',[]))
        frame('YGGDRASIL',network)
    if state.get('ssh'):
        frame('SSH',[
              ('ssh -6 -p '+str(state['ssh_port'])+' root@'+state['address'],'example'),
              ('Разрешённые источники подключения:','muted'),
              *((value,'value') for value in state['ssh_allowed']),
              ('root замените своим Linux-пользователем. Нужен его ключ или пароль VPS.','muted')])
    elif state.get('address'):
        frame('SSH',[('Доступ через Yggdrasil выключен. VPN работает независимо от SSH.','muted')])
    checks=[]
    if state.get('verification')=='listeners':
        checked=state.get('checked_protocols',[])
        checks.append(('TCP-слушатели проверены: '+', '.join(HOST_SUFFIXES[p] for p in checked)+'.','text') if checked else
                      ('TCP-проверки не выполнялись: в этом запуске выбран Hysteria2 (UDP).','muted'))
        if state.get('checked_at'): checks.append(('Время проверки: '+state['checked_at'],'muted'))
    elif state.get('verification')=='pending':
        checks.append(('Проверка TCP-слушателей не завершена. Откройте диагностику и повторите настройку.','warning'))
    else:
        checks.append(('Результат проверки слушателей в этом снимке не сохранён.','warning'))
    if any(host['protocol']=='hy2' for host in state['hosts']):
        checks.append(('Hysteria2 использует UDP; подключение проверяйте в VPN-клиенте.','text'))
    checks.append(('HTTPS и VPN-авторизацию проверьте в своём клиенте.','muted'))
    frame('ПРОВЕРКА',checks)
    frame('ЕСЛИ ПОДКЛЮЧЕНИЯ НЕТ',[
          ('1. Внутренние сквады: разрешите пользователю нужный inbound.','text'),
          ('2. Хосты: проверьте включённость Host и ограничения сквадов.','text'),
          ('3. Обновите подписку в клиенте; при ошибке откройте диагностику.','text')])
    if state.get('address'):
        frame('ТАХИОН / OPENWRT',[
              ('Если Ygg работает в другом клиенте, а в Тахионе не отвечает:','text'),
              ('проверьте bind_interface: "yggdrasil" у этого outbound на роутере.','example'),
              ('Некоторым версиям Тахиона нужна эта привязка; VLESS-подписка её не передаёт.','muted')],'warning')


def main(argv=None):
    parser=argparse.ArgumentParser(description='Remnawave: дополнительные протоколы в существующем профиле и сквадах')
    parser.add_argument('--version',action='version',version=VERSION)
    parser.add_argument('--plain',action='store_true',help='Обычный текстовый мастер вместо экранного меню')
    parser.add_argument('--panel'); parser.add_argument('--token-file',type=Path); parser.add_argument('--access-file',type=Path)
    parser.add_argument('--node'); parser.add_argument('--source-host'); parser.add_argument('--name')
    parser.add_argument('--protocol',action='append',choices=LABELS,default=[])
    parser.add_argument('--squad',action='append',default=[],help='UUID сквада для выбранных протоколов; повторяемый')
    parser.add_argument('--allow-ip',action='append',default=[])
    parser.add_argument('--access-mode',choices=('subscription','restricted'),help='Yggdrasil: по учётной записи или дополнительно по списку IP')
    parser.add_argument('--ssh-allow-ip',action='append',default=[])
    ssh=parser.add_mutually_exclusive_group(); ssh.add_argument('--ssh',action='store_true'); ssh.add_argument('--no-ssh',action='store_true')
    parser.add_argument('--ssh-port',type=int)
    parser.add_argument('--ygg-port',type=int,default=1488)
    parser.add_argument('--cert',type=Path); parser.add_argument('--key',type=Path)
    parser.add_argument('--peer',action='append',default=[])
    parser.add_argument('--firewall-ready',action='store_true')
    parser.add_argument('--yes',action='store_true'); parser.add_argument('--check',action='store_true')
    parser.add_argument('--result',action='store_true'); parser.add_argument('--network-only',action='store_true',help=argparse.SUPPRESS)
    parser.add_argument('--sync-certs',action='store_true',help=argparse.SUPPRESS)
    args=parser.parse_args(argv)
    if args.access_mode=='subscription' and args.allow_ip:
        parser.error('--allow-ip используется только с --access-mode restricted')
    if args.network_only: local.network_only(); return
    if args.sync_certs: certificates.sync(); return
    if args.result: result(); return
    local.preflight()
    if sys.stdin.isatty() and not ui.fullscreen(): termios.tcflush(sys.stdin.fileno(),termios.TCIFLUSH)
    frame('RemnaYgg · дополнительные протоколы · '+VERSION,[
        ('Мастер для VPS с уже работающей нодой Remnawave.','text'),
        ('Вы выбираете протоколы и группы пользователей. Скрипт готовит VPS,','text'),
        ('добавляет inbound и Host через API, затем выдаёт результат.','text'),
        ('До подтверждения плана рабочие настройки не применяются.','muted')])
    say('  Цвета: разделы — бирюзовые; вопросы — жёлтые; выбранное — зелёное.','muted')
    say('  Примеры и команды — синие; замечания — янтарные; ошибки — красные.','muted')
    old=json.loads((local.BASE/'multi-state.json').read_text()) if (local.BASE/'multi-state.json').exists() else {}
    if args.ssh_port is None: args.ssh_port=old.get('ssh_port',22)
    panel=args.panel or old.get('panel')
    section(1,'Доступ к панели')
    if not panel and not args.yes:
        say('  Укажите адрес панели Remnawave, в которой настроена эта нода.\n  Нужен корневой адрес сайта, без /dashboard и без ссылки подписки.')
        say('  Пример: https://panel.example.com','example')
        panel=prompt('Адрес панели Remnawave')
    if not panel: raise Error('Укажите --panel')
    token=ui.env_secret('REMNAWAVE_API_TOKEN')
    if args.token_file:
        if args.token_file.stat().st_mode & 0o077: raise Error('Закройте файл токена: chmod 600')
        token=args.token_file.read_text().strip()
    if not token and not args.yes:
        ui.page('1 · API-токен')
        say('  Где взять токен: «Настройки Remnawave» → «Токены API».\n  Создайте отдельный временный токен с чтением и изменением профилей,\n  нод, Host и внутренних сквадов. Подойдёт временный полный доступ.')
        say('  Токен используется только в этом запуске и не сохраняется.','muted')
        token=secret_prompt('Вставьте API-токен панели')
    api=API(panel,token)
    secret=ui.env_secret('REMNAWAVE_PANEL_ACCESS')
    if args.access_file:
        if args.access_file.stat().st_mode & 0o077: raise Error('Закройте файл доступа: chmod 600')
        secret=args.access_file.read_text().strip()
    if secret: api.set_access(secret)
    try: nodes=api.nodes()
    except AccessRequired:
        if args.yes or secret: raise
        ui.page('1 · Защита входа eGames')
        say('  Перед API обнаружена страница входа или HTML-заглушка.','warning')
        say('  При cookie-защите eGames нужна исходная секретная ссылка из\n  результата установки или меню eGames. После входа браузер обычно\n  убирает секрет из адресной строки. Обычный адрес панели не подходит.')
        say('  Пример: https://panel.example.com/auth/login?ИМЯ=ЗНАЧЕНИЕ','example')
        say('  Можно ввести Cookie в формате name=value. Ссылка не сохраняется.','muted')
        api.set_access(secret_prompt('Вставьте полную секретную ссылку eGames или Cookie'))
        nodes=api.nodes()
    section(2,'Нода и исходное подключение')
    say('  Выберите в панели именно ту VPS, на которой запущен мастер.\n  Нода должна быть подключена к панели, а её обычный VLESS — работать.')
    selected=choose(nodes,args.node,old.get('node_uuid'),args.yes,'Нода ЭТОГО VPS')
    hosts=api.hosts()
    available=[h for h in hosts if h.profile==selected.profile and h.inbound in {i.uuid for i in selected.inbounds}
               and (not h.nodes or selected.uuid in h.nodes) and not h.raw.get('isDisabled') and not h.raw.get('isHidden')
               and not any(t.startswith('REMNA_MULTI_') or t=='YGGDRASIL_NATIVE' for t in h.raw.get('tags',[]))]
    profile=obj(api.request('GET','/config-profiles/'+selected.profile))
    shared_nodes=[string(d,'name') for value in seq(profile['nodes'])
                  if uid(d:=obj(value),'uuid')!=selected.uuid]
    typed=Profile.parse(profile['config'])
    base_ids={i.uuid for i in selected.inbounds if i.tag in typed.bases}
    ui.page('2 · Исходное подключение')
    say('  Выберите рабочий TCP Reality Host этой ноды.\n  Его параметры станут основой дополнительных подключений.')
    source=choose([h for h in available if h.inbound in base_ids],args.source_host,old.get('source_host_uuid'),args.yes,'Исходный VLESS TCP Reality Host')
    base=next(i.tag for i in selected.inbounds if i.uuid==source.inbound)
    field('Адрес API выбранной ноды',selected.address)
    field('Профиль',profile['name'])
    field('Исходный inbound',base)
    section(3,'Какие подключения добавить')
    say('  Обычный TCP Reality сохраняется. Дополнения можно выбирать независимо.\n  Если такой протокол уже есть в профиле, мастер использует его настройки.')
    say('  Отметьте дополнения пробелом и нажмите Enter.' if ui.fullscreen() else
        '  Пример ввода: 4 — только Yggdrasil; 1 4 — Hysteria2 и Yggdrasil.','example')
    protocols=args.protocol
    if not protocols and not args.yes:
        protocols=ui.multi_select('Что добавить?',[
            ('hy2','Hysteria2 · UDP 443 · нужен сертификат'),
            ('grpc','gRPC Reality · TCP 8443'),
            ('xhttp','XHTTP Reality · TCP 4443'),
            ('ygg','Yggdrasil Reality · TCP 1488')],['ygg'],allow_empty=False)
    config,mapping=convert(typed,base,protocols,{'ygg':args.ygg_port})
    raw_by_tag={i['tag']:i for i in config['inbounds']}
    name=args.name or old.get('name') or selected.name
    if not args.yes and not args.name:
        ui.page('3 · Название подключений')
        say('  Общее название без протокола. Например: 🇳🇱 Нидерланды.\n  Мастер добавит короткое окончание Histeria, GRPC, XHTTP или Ygg.','example')
        name=prompt('Общее название сервера',name)
    if not 1<=len(name)<=60 or any(c in name for c in '\r\n'): raise Error('Название должно содержать от 1 до 60 символов')
    all_squads=provision.squads(api)
    known={s['uuid'] for s in all_squads}
    if any(v not in known for v in args.squad): raise Error('Не найден указанный UUID сквада')
    grants={p:list(args.squad) for p in protocols}
    section(4,'Кому выдать доступ')
    say('  Внутренний сквад — группа VPN-пользователей в Remnawave.\n  Для каждого дополнения выберите группы, которым оно нужно.\n  Мастер добавит им доступ к inbound и ограничит ими показ нового Host.')
    say('  Enter без выбора → новый Host будет создан выключенным.\n  Позже назначьте ему группы в панели и включите Host.','warning')
    say('  У уже существующих Host сохраняются ограничения, заданные в панели.\n  Выбор ниже добавляет доступ к inbound, но не меняет ограничения такого Host.','muted')
    for p in protocols:
        if args.yes or args.squad: continue
        if ui.fullscreen():
            ui.page('4 · Доступ · '+HOST_SUFFIXES[p])
            say('  Отметьте группы VPN-пользователей для этого подключения.\n  Без выбора новый Host будет выключен: доступ можно настроить в панели.')
        say('\n  '+LABELS[p],'section')
        grants[p]=ui.multi_select('Каким сквадам выдать доступ?',
                                 [(s['uuid'],s['name']) for s in all_squads],[])
    # Omitting Yggdrasil means leave it alone, including the saved SSH/access state.
    allowed=old.get('allowed',[]); ssh_allowed=old.get('ssh_allowed',[]); ssh_enabled=old.get('ssh',False)
    access_mode=old.get('access_mode','restricted' if allowed else 'subscription')
    restricted_allowed=old.get('restricted_allowed',allowed)
    if 'ygg' in protocols:
        section(5,'Устройства Yggdrasil и доступ по SSH')
        say('  VPN проверяет учётную запись Remnawave и доступ к inbound.\n  Выберите, нужно ли дополнительно ограничивать IPv6 устройств.')
        if not ui.fullscreen():
            option(1,'По подписке','любой адрес Yggdrasil; IP клиентов вводить не нужно')
            option(2,'По подписке и списку IP','дополнительное ограничение устройств')
        access_mode=args.access_mode or ('restricted' if args.allow_ip else access_mode)
        if not args.yes and not args.access_mode and not args.allow_ip:
            if ui.fullscreen():
                access_mode=ui.screen.select('Режим доступа к VPN',[
                    ('subscription','По подписке — без списка IP'),('restricted','По подписке и списку IP')],access_mode)
            else:
                while True:
                    answer=prompt('Режим доступа к VPN','1' if access_mode=='subscription' else '2')
                    if answer in ('1','2'):
                        access_mode='subscription' if answer=='1' else 'restricted'; break
                    say('  Введите 1 или 2.','error')
        if access_mode=='subscription':
            allowed=[]
            say('  IP клиентов не ограничиваются. Нужны рабочая сеть Yggdrasil\n  и действующая VPN-учётная запись с доступом к этому inbound.','success')
        else:
            frame('Два разных поля на роутере',[
                ('IPv6: адрес самого роутера. Уберите окончание /7.','text'),
                ('200:1111:2222:3333:4444:5555:6666:7777','example'),
                ('IPv6-PD: подсеть всей LAN. Оставьте окончание /64.','text'),
                ('300:1111:2222:3333::/64','example'),
                ('Это примеры. Вставляйте свои значения из интерфейса роутера.','warning')])
            say('  Где взять: OpenWrt → Сеть → Интерфейсы → Yggdrasil.\n  Подсеть LAN охватывает ПК, даже если его IPv6 меняется.\n  Для друзей можно добавить несколько значений через пробел.\n  Адрес самой VPS в эти списки не вводится.')
            if args.allow_ip or args.yes: allowed=local.networks(args.allow_ip or restricted_allowed,pair=True)
            else:
                allowed=access([],[v for v in restricted_allowed if v.startswith('2')],False,'IPv6 роутеров для VPN — без /7','router')
                allowed+=access([],[v for v in restricted_allowed if v.startswith('3')],False,'Подсети LAN для VPN — с /64','lan')
            restricted_allowed=allowed
        ssh_enabled=args.ssh or (old.get('ssh',False) and not args.no_ssh)
        if not args.yes and not args.ssh and not args.no_ssh:
            ui.page('5 · SSH через Yggdrasil')
            say('\n  SSH позволяет управлять VPS через Yggdrasil. Для VPN он не обязателен.')
            ssh_enabled=confirm('Разрешить также SSH через Yggdrasil?',ssh_enabled)
        if ssh_enabled:
            ui.page('5 · Доступ к SSH')
            say('\n  Кому разрешить управление VPS по SSH','section')
            say('  Это отдельный список. Например, другу можно выдать VPN, а SSH — только\n  себе. Для входа всё равно потребуется существующий ключ или пароль VPS.\n  Enter оставит предложенные ниже адреса. Можно ввести свой список.')
            say('  Пример: IPv6 роутера 200:1111:2222:3333:4444:5555:6666:7777\n  или подсеть вашей LAN 300:1111:2222:3333::/64.\n  OpenWrt → Сеть → Интерфейсы → Yggdrasil: IPv6 без /7, IPv6-PD с /64.','example')
            ssh_allowed=access(args.ssh_allow_ip,old.get('ssh_allowed') or allowed,args.yes,'Разрешённые IP и подсети для SSH')
        else:
            ssh_allowed=[]
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
    cert=args.cert; key=args.key; certificate_managed=False; retire_cert_writer=False
    cert_targets=(certificates.CERT_TARGET,certificates.KEY_TARGET)
    if 'hy2' in protocols:
        hy2=raw_by_tag[mapping['hy2']]
        certificate_managed=hy2['tag'] not in old_tags
        if not certificate_managed:
            configured=certificates.configured_targets(hy2)
            cert_targets=tuple(Path(v) for v in configured)
            certificate_managed=cert_targets==certificates.managed_targets()
            if not certificate_managed:
                if cert or key: raise Error('Существующий Hysteria2 использует свой сертификат. --cert/--key не меняют его; настройте его прежним способом.')
                pair=certificates.configured_pair(configured,docker['Mounts'],source.address)
                retire_cert_writer=certificates.managed_targets() is not None
                say('  Существующий Hysteria2: проверен его сертификат из mount контейнера.\n  Прежний способ обновления сертификата сохраняется.','success')
            else:
                saved=json.loads((local.BASE/'cert-sync.json').read_text())
                cert=cert or Path(saved['cert']); key=key or Path(saved['key'])
                # A private profile can safely move our old shared filenames to dedicated files.
                # In a shared profile other nodes may still require the old paths.
                if not shared_nodes and cert_targets==(certificates.LEGACY_CERT_TARGET,certificates.LEGACY_KEY_TARGET):
                    cert_targets=(certificates.CERT_TARGET,certificates.KEY_TARGET)
                    entry=hy2['streamSettings']['tlsSettings']['certificates'][0]
                    entry.update(certificateFile=str(cert_targets[0]),keyFile=str(cert_targets[1]))
                    say('  Наш прежний Hysteria2 перейдёт на отдельные файлы сертификата.','warning')
        if certificate_managed:
            if not any(m['Destination']=='/dev/shm' and m['Source']=='/dev/shm' and m['RW'] for m in docker['Mounts']):
                raise Error('Для нового Hysteria2 требуется общий доступный для записи /dev/shm:/dev/shm в Compose ноды')
            if docker['Config'].get('User') not in ('','0','root','0:0'):
                raise Error('Новый Hysteria2: эта версия ожидает root внутри контейнера для чтения ключа 600')
            if not cert:
                section(6,'Сертификат для Hysteria2')
                say('  Проверяю существующие сертификаты Certbot. Мастер выберет тот,\n  который подходит домену Host и ещё действует.')
                field('Домен Host',source.address)
                detected=certificates.discover(source.address)
                if detected:
                    cert,detected_key=detected
                    key=key or detected_key
                    field('Сертификат найден',str(cert))
            if not cert and not args.yes:
                say('  Подходящий действующий сертификат автоматически не найден.','warning')
                say('  Он нужен только Hysteria2. Для VLESS Reality через Yggdrasil\n  TLS-сертификат не требуется. Если сертификат хранится вне Certbot,\n  введите путь к fullchain.pem. Имя папки может отличаться от домена.\n  Подойдёт сертификат этого домена или соответствующий wildcard.')
                say('  Пример: /etc/letsencrypt/live/example.com/fullchain.pem','example')
                cert=Path(prompt('Путь к полному сертификату'))
            if not cert: raise Error('Укажите --cert и --key для Hysteria2')
            key=key or cert.parent/'privkey.pem'
            pair=certificates.validate(cert,key,source.address)
        field('Сертификат проверен',str(pair.cert))
    ui.page('7 · План применения')
    frame('ПЛАН ПРИМЕНЕНИЯ',[(selected.name+' · '+selected.address,'value'),('Профиль: '+profile['name'],'text')])
    for p,tag in mapping.items():
        names=[s['name'] for s in all_squads if s['uuid'] in grants[p]]
        say('\n  '+LABELS[p],'section')
        field('Inbound',tag)
        field('Название нового Host',name+' '+HOST_SUFFIXES[p])
        field('Порт',str(raw_by_tag[tag]['port']))
        field('Добавить доступ сквадам',', '.join(names) or 'не добавлять')
        if not names: say('  Новый Host будет выключен; существующий Host сохранит настройки.','warning')
    if 'ygg' in protocols:
        field('Доступ к VPN','по подписке, без списка IP' if access_mode=='subscription' else 'по подписке и списку IP')
        if allowed: field('Устройства для VPN',' '.join(allowed))
        field('SSH через Yggdrasil','включён' if ssh_enabled else 'выключен')
        if ssh_enabled: field('Устройства для SSH',' '.join(ssh_allowed))
    say('\n  Применение профиля и прав сквадов может кратко перезагрузить Xray.','warning')
    if shared_nodes:
        frame('Общий профиль — возможен перезапуск других нод',[
            ('Этот профиль также используют: '+', '.join(shared_nodes),'warning'),
            ('Remnawave может перезапустить Xray на всех нодах этого профиля.','text'),
            ('Возможен короткий разрыв соединений. Их активные inbound сохраняются.','text'),
            ('Для независимых изменений используйте отдельный профиль для ноды.','example')],'warning')
        say('  Если выбран уже существующий inbound, право сквада действует на всех\n  нодах с этим inbound. Ограничение Host определяет показ в подписке;\n  оно не ограничивает применение уже выданных реквизитов одной нодой.','warning')
    say('  Если профиль общий, новые inbound активируются только на выбранной ноде.\n  Прежние inbound, Host и разрешения сохраняются. Отсутствие протокола\n  в текущем выборе не удаляет уже настроенное подключение.','muted')
    if args.check: say('\n  Проверка завершена. План корректен; рабочие настройки не применялись.','success'); return
    if not args.yes and not confirm('Применить показанный план?'):
        ui.page('Настройка отменена')
        say('  Отменено. Рабочие настройки не применялись.','warning'); return
    ui.begin_apply()
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
        section(1,'Применение: зависимости')
        say('  Устанавливаю недостающие пакеты. Это может занять несколько минут.','muted')
        os.environ['DEBIAN_FRONTEND']='noninteractive'
        local.run('apt-get','update',timeout=300)
        local.run('apt-get','install','-y','python3','curl','ca-certificates','openssl','iproute2','nftables','kmod',timeout=300)
        address=old.get('address','')
        if 'ygg' in protocols:
            section(2,'Применение: Yggdrasil и сетевой доступ')
            local.run('modprobe','tun',check=False)
            address=local.setup_ygg(args.peer,backup)
            legacy=json.loads((local.BASE/'state.json').read_text()) if (local.BASE/'state.json').exists() else {}
            extra=tuple({legacy['port']} if legacy.get('address')==address else set())
            local.firewall(address,raw_by_tag[mapping['ygg']]['port'],allowed,args.ssh_port,ssh_allowed,
                           args.firewall_ready,exclusive=True,extra_vpn_ports=extra,
                           subscription=access_mode=='subscription',extra_allowed=legacy.get('allowed',[]))
            if ssh_enabled:
                local.run('apt-get','install','-y','openssh-server',timeout=300)
                local.ssh_config(address,args.ssh_port)
        else: say('  Yggdrasil не выбран: прежние настройки сети и SSH сохраняются.','muted')
        ufw=bool(shutil.which('ufw') and 'Status: active' in local.run('ufw','status'))
        for p,tag in mapping.items():
            if p!='ygg' and ufw: local.run('ufw','allow',str(raw_by_tag[tag]['port'])+('/udp' if p=='hy2' else '/tcp'))
        if certificate_managed: certificates.setup(pair,*cert_targets)
        elif retire_cert_writer:
            local.run('systemctl','disable','--now','remna-hy2-certs.timer')
            local.run('systemctl','stop','remna-hy2-certs.service')
        section(3,'Применение: профиль, Host и сквады')
        # Root-only journal is useful if an API call times out after being applied.
        local.write(backup/'plan.json',json.dumps({'node':selected.uuid,'mapping':mapping,'grants':grants}))
        provision.apply(api,selected,profile['config'],config,mapping,source,address,name,grants,backup,old.get('name'))
        current_profile=obj(api.request('GET','/config-profiles/'+selected.profile))
        tags={i['uuid']:i['tag'] for i in current_profile['inbounds']}
        inventory=[]
        for host in api.hosts():
            protocol=next((p for p in LABELS if 'REMNA_MULTI_'+p.upper() in host.raw.get('tags',[])),None)
            if protocol and host.nodes==(selected.uuid,) and host.profile==selected.profile:
                inventory.append(dict(protocol=protocol,tag=tags[host.inbound],host_uuid=host.uuid,
                                      name=host.name,address=host.address,port=host.port,disabled=host.raw['isDisabled']))
        state=dict(panel=panel,name=name,node_uuid=selected.uuid,profile=selected.profile,profile_name=profile['name'],source_host_uuid=source.uuid,
                   address=address,allowed=allowed,access_mode=access_mode,restricted_allowed=restricted_allowed,
                   ssh=ssh_enabled,ssh_allowed=ssh_allowed,ssh_port=args.ssh_port,hosts=inventory,
                   saved_at=datetime.datetime.now(datetime.timezone.utc).isoformat(timespec='seconds'),verification='pending')
        local.write(local.BASE/'multi-state.json',json.dumps(state,ensure_ascii=False,indent=2))
        local.write(Path('/usr/local/sbin/remnawave-yggdrasil'),f'#!/bin/sh\nexec /usr/bin/python3 {local.APP}/multi.py "$@"\n',0o700)
        section(4,'Применение: проверка TCP-слушателей')
        for p,tag in mapping.items():
            if p=='hy2': continue
            target=address if p=='ygg' else '127.0.0.1'
            for attempt in range(30):
                try:
                    with socket.create_connection((target,raw_by_tag[tag]['port']),timeout=2): break
                except OSError:
                    if attempt==29: raise Error('Не появился слушатель '+tag+'. Проверьте журнал ноды и профиль; уже созданные настройки сохранены.') from None
                    time.sleep(2)
        state.update(verification='listeners',checked_protocols=[p for p in mapping if p!='hy2'],
                     checked_at=datetime.datetime.now(datetime.timezone.utc).isoformat(timespec='seconds'))
        local.write(local.BASE/'multi-state.json',json.dumps(state,ensure_ascii=False,indent=2))
    result(after_install=True)

if __name__=='__main__':
    os.umask(0o077); os.environ['LC_ALL']='C'
    try:
        from screen import available,launch
        if not available(sys.argv[1:]) or not launch(main,result,VERSION,sys.argv[1:]): main()
    except (Error,OSError,ValueError,KeyError,subprocess.SubprocessError) as exc:
        say('\n  Установка остановлена: '+str(exc),'error')
        say('  До подтверждения плана рабочие настройки не применяются. Если ошибка\n  возникла после подтверждения, часть шагов могла завершиться. Исправьте\n  указанную причину и повторите запуск: существующие Host не дублируются.','warning')
        say('  Токен и секретная ссылка не сохранялись. Резервные копии и план:','muted')
        say('  /etc/remnawave-yggdrasil/backups','example')
        say('  Журналы:','muted')
        say('  journalctl -u yggdrasil -u remna-ygg-access -u remna-hy2-certs\n  docker logs --tail 80 remnanode','example')
        sys.exit(1)
    except KeyboardInterrupt:
        say('\n  Остановлено пользователем. Если применение уже началось, часть шагов\n  могла завершиться; повторный запуск сверится с текущими настройками.','warning')
        sys.exit(130)
