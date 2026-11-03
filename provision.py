"""Panel-managed inbounds, activation and squad access. No subscriber credentials."""
import copy
import json
from pathlib import Path
from api import API, Error, Host, Node, obj, seq, string, uid
from converter import LABELS, HOST_SUFFIXES


def squads(api: API) -> list[dict]:
    response=obj(api.request('GET','/internal-squads'))
    return [dict(uuid=uid(d,'uuid'),name=string(d,'name'),inbounds=[uid(obj(i),'uuid') for i in seq(d.get('inbounds'))])
            for value in seq(response.get('internalSquads')) if (d:=obj(value))]


def apply(api: API, node: Node, before: dict, config: dict, mapping: dict[str,str],
          source: Host, address: str, name: str, grants: dict[str,list[str]], backup: Path,
          previous_name: str | None = None) -> list[dict]:
    """Preserve panel edits; only migrate names that exactly match our generated format."""
    selected=Node.parse(api.request('GET','/nodes/'+node.uuid),require_connected=False)
    if selected.profile!=node.profile:
        raise Error('Активный профиль ноды изменился. Настройки панели не менялись; повторите запуск.')
    current=obj(api.request('GET','/config-profiles/'+node.profile))
    if current['config'] != before:
        raise Error('Профиль изменился во время установки. Повторите запуск, чтобы сохранить чужие изменения.')
    backup.mkdir(parents=True,exist_ok=True,mode=0o700)
    (backup/'profile-before.json').write_text(json.dumps(current,ensure_ascii=False,indent=2),encoding='utf-8')
    (backup/'profile-before.json').chmod(0o600)
    if config!=before:
        api.request('PATCH','/config-profiles',{'uuid':node.profile,'config':config})
    # A lost PATCH response is resolved by a normal rerun; no blind mutation retries.
    updated=obj(api.request('GET','/config-profiles/'+node.profile))
    identifiers={string(obj(i),'tag'):uid(obj(i),'uuid') for i in seq(updated.get('inbounds'))}
    # Applying a profile can briefly reconnect the node. Read its stored binding directly,
    # rather than filtering it out of the connected-node selection list during our reload.
    latest=Node.parse(api.request('GET','/nodes/'+node.uuid),require_connected=False)
    if latest.profile!=node.profile: raise Error('Активный профиль ноды изменился. Новые Host не создавались.')
    active=[i.uuid for i in latest.inbounds]
    for tag in mapping.values():
        if tag not in identifiers: raise Error('Панель не создала inbound: '+tag)
        if identifiers[tag] not in active: active.append(identifiers[tag])
    if active != [i.uuid for i in latest.inbounds]:
        api.request('PATCH','/nodes',{'uuid':node.uuid,'configProfile':{
            'activeConfigProfileUuid':node.profile,'activeInbounds':active}})
    all_hosts=api.hosts()
    result=[]
    for protocol,tag in mapping.items():
        identifier=identifiers[tag]
        marker='REMNA_MULTI_'+protocol.upper()
        existing=[h for h in all_hosts if marker in h.raw.get('tags',[]) and h.nodes==(node.uuid,) and h.inbound==identifier]
        if len(existing)>1: raise Error('Дублируются управляемые Host для '+tag)
        raw=next(i for i in config['inbounds'] if i['tag']==tag)
        selected=grants[protocol]
        # Read each squad just before changing it; keep every unrelated inbound.
        for squad_uuid in selected:
            squad=obj(api.request('GET','/internal-squads/'+squad_uuid))
            inbounds=[uid(obj(i),'uuid') for i in seq(squad.get('inbounds'))]
            if identifier not in inbounds:
                api.request('PATCH','/internal-squads',{'uuid':squad_uuid,'inbounds':inbounds+[identifier]})
        if existing:
            host=existing[0]
            generated={prefix+separator+suffix for prefix in (name,previous_name or name)
                       for separator,suffix in ((' · ',LABELS[protocol]),(' ',HOST_SUFFIXES[protocol]))}
            remark=name+' '+HOST_SUFFIXES[protocol]
            if host.name in generated and host.name!=remark:
                host=Host.parse(api.request('PATCH','/hosts',{'uuid':host.uuid,'remark':remark}))
            # Only the generated name may change; SNI, access and manual names remain authoritative.
            result.append(dict(protocol=protocol,tag=tag,host_uuid=host.uuid,name=host.name,address=host.address,port=host.port))
            continue
        stream=raw['streamSettings']
        sni=stream['realitySettings']['serverNames'][0] if protocol!='hy2' else source.address
        payload={'inbound':{'configProfileUuid':node.profile,'configProfileInboundUuid':identifier},
                 'remark':name+' '+HOST_SUFFIXES[protocol], 'address':address if protocol=='ygg' else source.address,
                 'port':raw['port'],'sni':sni,'fingerprint':'chrome' if protocol!='hy2' else None,
                 'nodes':[node.uuid],'tags':[marker],'isDisabled':not selected,'isHidden':False,
                 'overrideSniFromAddress':False,'keepSniBlank':False,
                 'internalSquads':{'mode':'ALLOW_ONLY' if selected else 'EXCLUDE','squads':selected}}
        if protocol=='grpc': payload['path']=stream.get('grpcSettings',{}).get('serviceName','')
        if protocol=='xhttp': payload['path']=stream.get('xhttpSettings',{}).get('path','/')
        if protocol=='hy2': payload['alpn']='h3'
        host=Host.parse(api.request('POST','/hosts',payload))
        all_hosts.append(host)
        result.append(dict(protocol=protocol,tag=tag,host_uuid=host.uuid,name=host.name,address=host.address,port=host.port))
    return result
