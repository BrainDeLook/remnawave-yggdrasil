"""Offline, non-destructive Remnawave profile conversion. No network dependencies."""
from __future__ import annotations
import argparse
import copy
from dataclasses import dataclass
import json
from pathlib import Path

class ConversionError(ValueError):
    pass

LABELS = {'hy2':'Hysteria2', 'grpc':'VLESS gRPC Reality',
          'xhttp':'VLESS XHTTP Reality', 'ygg':'VLESS Reality Yggdrasil'}
HOST_SUFFIXES = {'hy2':'Histeria', 'grpc':'GRPC', 'xhttp':'XHTTP', 'ygg':'Ygg'}
PORTS = {'hy2':443, 'grpc':8443, 'xhttp':4443, 'ygg':1488}

@dataclass(frozen=True)
class Profile:
    config: dict
    bases: tuple[str, ...]

    @classmethod
    def parse(cls, value: object) -> Profile:
        if not isinstance(value, dict) or not isinstance(value.get('inbounds'), list):
            raise ConversionError('Нужен полный JSON профиля с массивом inbounds')
        routing = value.get('routing', {})
        if not isinstance(routing, dict) or not isinstance(routing.get('rules', []), list):
            raise ConversionError('В routing ожидается объект с массивом rules')
        for rule in routing.get('rules', []):
            if not isinstance(rule, dict):
                raise ConversionError('Правило routing должно быть объектом')
            if 'inboundTag' in rule and (not isinstance(rule['inboundTag'], list) or
                    not all(isinstance(tag, str) for tag in rule['inboundTag'])):
                raise ConversionError('В routing inboundTag должен быть массивом строк')
        tags = set()
        bases = []
        for inbound in value['inbounds']:
            if not isinstance(inbound, dict) or not isinstance(inbound.get('tag'), str) or not inbound['tag']:
                raise ConversionError('У каждого inbound должен быть непустой tag')
            tag = inbound['tag']
            if tag in tags:
                raise ConversionError('Повторяется tag inbound: '+tag)
            tags.add(tag)
            if type(inbound.get('port')) is not int or not 1 <= inbound['port'] <= 65535:
                raise ConversionError('Некорректный порт inbound: '+tag)
            stream = inbound.get('streamSettings', {})
            if not isinstance(stream, dict): raise ConversionError('Некорректный streamSettings: '+tag)
            if inbound.get('protocol') == 'vless' and stream.get('network','tcp') in ('tcp','raw') and stream.get('security') == 'reality':
                reality = stream.get('realitySettings')
                if not isinstance(reality, dict) or not isinstance(reality.get('serverNames'), list) or not reality['serverNames'] or not all(isinstance(n,str) and n for n in reality['serverNames']):
                    raise ConversionError('В Reality нужны непустые serverNames: '+tag)
                if not isinstance(reality.get('privateKey'),str) or not reality['privateKey'] or not isinstance(reality.get('shortIds'),list):
                    raise ConversionError('В Reality нужны privateKey и shortIds: '+tag)
                if not reality.get('dest') and not reality.get('target'):
                    raise ConversionError('В Reality нужен dest или target: '+tag)
                if not isinstance(inbound.get('settings'),dict): raise ConversionError('Отсутствует settings: '+tag)
                bases.append(tag)
        return cls(copy.deepcopy(value),tuple(bases))


def convert(profile: Profile, base_tag: str, protocols: list[str], ports: dict[str,int] | None = None) -> tuple[dict, dict[str,str]]:
    """Append missing transports; existing inbounds (including edits in panel) stay intact."""
    if base_tag not in profile.bases: raise ConversionError('Выберите базовый VLESS TCP/RAW Reality inbound')
    if not protocols or any(p not in LABELS for p in protocols): raise ConversionError('Выберите протоколы')
    ports = PORTS | (ports or {})
    if any(type(p) is not int or not 1 <= p <= 65535 for p in ports.values()): raise ConversionError('Порт должен быть от 1 до 65535')
    result = copy.deepcopy(profile.config)
    base = next(i for i in result['inbounds'] if i['tag']==base_tag)
    mapping = {}
    created = []
    for protocol in dict.fromkeys(protocols):
        tag = base_tag+'-'+protocol if protocol!='ygg' else base_tag+'-yggdrasil'
        matches = [i for i in result['inbounds'] if i['tag']==tag]
        if matches:
            current = matches[0]
            stream = current.get('streamSettings',{})
            expected = 'tcp' if protocol=='ygg' else 'hysteria' if protocol=='hy2' else protocol
            if stream.get('network','tcp') not in ((expected,'raw') if protocol=='ygg' else (expected,)) or current.get('protocol') != ('hysteria' if protocol=='hy2' else 'vless'):
                raise ConversionError('Tag уже занят другим протоколом: '+tag)
            if protocol!='hy2' and stream.get('security')!='reality': raise ConversionError('Ожидался Reality: '+tag)
            mapping[protocol]=tag
            continue
        # Existing upstream multi-protocol inbounds can be reused; do not duplicate ports.
        candidates = [i for i in result['inbounds'] if protocol!='ygg' and i.get('streamSettings',{}).get('network') == ('hysteria' if protocol=='hy2' else protocol)
                      and i.get('protocol') == ('hysteria' if protocol=='hy2' else 'vless')
                      and (protocol=='hy2' or i['streamSettings'].get('security')=='reality')]
        if len(candidates)>1: raise ConversionError('Несколько существующих inbound для '+LABELS[protocol]+'. Используйте выборочный профиль.')
        if candidates:
            mapping[protocol]=candidates[0]['tag']; continue
        port = ports[protocol]
        udp = protocol=='hy2'
        for existing in result['inbounds']:
            existing_udp = existing.get('protocol')=='hysteria'
            if existing['port']==port and existing_udp==udp:
                raise ConversionError(f'Порт {port}/{"udp" if udp else "tcp"} уже занят inbound {existing["tag"]}. Выберите другой порт.')
        if protocol=='hy2':
            inbound = {'tag':tag,'listen':'0.0.0.0','port':port,'protocol':'hysteria',
                       'settings':{'clients':[],'version':2},'streamSettings':{
                           'network':'hysteria','security':'tls','hysteriaSettings':{'version':2},
                           'tlsSettings':{'alpn':['h3'],'certificates':[{
                               'certificateFile':'/dev/shm/remna-ygg-hy2-cert.pem','keyFile':'/dev/shm/remna-ygg-hy2-key.pem'}]}}}
        else:
            inbound = copy.deepcopy(base)
            inbound.update(tag=tag,port=port,listen='::' if protocol=='ygg' else '0.0.0.0')
            inbound['settings']['clients']=[]
            stream = inbound['streamSettings']
            for key in ('tcpSettings','rawSettings','grpcSettings','xhttpSettings','sockopt'):
                stream.pop(key,None)
            stream['network']='tcp' if protocol=='ygg' else protocol
            if protocol in ('grpc', 'xhttp'):
                inbound['settings'].pop('flow', None)
            if protocol=='grpc': stream['grpcSettings']={'serviceName':tag.replace('-','_'),'multiMode':False}
            if protocol=='xhttp': stream['xhttpSettings']={'path':'/','mode':'auto'}
        result['inbounds'].append(inbound)
        created.append(tag)
        mapping[protocol]=tag
    # New VPN listeners inherit source-specific exit/blocking policies. Existing
    # transports retain the policy the administrator has already set in the panel.
    for rule in result.get('routing', {}).get('rules', []):
        if base_tag in rule.get('inboundTag', []):
            rule['inboundTag'].extend(tag for tag in created if tag not in rule['inboundTag'])
    return result,mapping


def main():
    parser=argparse.ArgumentParser(description='Локальный конвертер профилей Remnawave, без сети')
    parser.add_argument('input',type=Path); parser.add_argument('output',type=Path)
    parser.add_argument('--base',required=True,help='tag базового VLESS TCP Reality inbound')
    parser.add_argument('--protocol',action='append',choices=LABELS,required=True)
    parser.add_argument('--ygg-port',type=int,default=1488)
    args=parser.parse_args()
    if args.input.resolve()==args.output.resolve(): parser.error('Сохраните результат в другой файл')
    profile=Profile.parse(json.loads(args.input.read_text(encoding='utf-8-sig')))
    result,mapping=convert(profile,args.base,args.protocol,{'ygg':args.ygg_port})
    with args.output.open('x',encoding='utf-8') as file: json.dump(result,file,ensure_ascii=False,indent=2)
    print('\n'.join(LABELS[p]+': '+tag for p,tag in mapping.items()))

if __name__=='__main__': main()
