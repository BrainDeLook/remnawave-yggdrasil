"""Disposable VM integration: real panel + real node; never imports real user data."""
import base64
import datetime
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import tempfile
import time
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from api import API, Error
from node import run
from probe import export, verify
ROOT=Path(__file__).resolve().parents[1]


def wait(action,seconds=180):
    deadline=time.monotonic()+seconds
    while time.monotonic()<deadline:
        try:
            value=action()
            if value: return value
        except (Error,OSError): pass
        time.sleep(2)
    raise RuntimeError('CI stack did not become ready')


def main():
    os.umask(0o077)
    secret=secrets.token_hex(32)
    # Test databases are isolated containers; the panel/node use host mode like eGames.
    run('docker','run','-d','--name','test-remna-db','-p','127.0.0.1:5433:5432',
        '-e','POSTGRES_PASSWORD='+secret,'postgres:17-alpine',timeout=300)
    run('docker','run','-d','--name','test-remna-redis','-p','127.0.0.1:6378:6379','valkey/valkey:8-alpine',timeout=300)
    wait(lambda: 'accepting connections' in run('docker','exec','test-remna-db','pg_isready','-U','postgres',check=False))
    env={'APP_PORT':'3000','METRICS_PORT':'3001','API_INSTANCES':'1',
         'DATABASE_URL':f'postgresql://postgres:{secret}@127.0.0.1:5433/postgres',
         'REDIS_HOST':'127.0.0.1','REDIS_PORT':'6378','APP_SECRET':secret,
         'JWT_AUTH_SECRET':secrets.token_hex(32),'JWT_API_TOKENS_SECRET':secrets.token_hex(32),
         'FRONT_END_DOMAIN':'*','PANEL_DOMAIN':'localhost','SUB_PUBLIC_DOMAIN':'localhost:3000/api/sub',
         'IS_TELEGRAM_NOTIFICATIONS_ENABLED':'false','METRICS_USER':'ci','METRICS_PASS':secret}
    os.environ.update(env)
    run('docker','run','-d','--name','test-remnawave','--network','host',
        *[a for key in env for a in ('-e',key)],'remnawave/backend:3',timeout=300)
    api=API('http://127.0.0.1:3000','ci-bootstrap')
    wait(lambda: api.request('GET','/auth/status'))
    admin=api.request('POST','/auth/register',{'username':'ciadmin','password':'Aa1'+secrets.token_hex(20)})
    api.token=admin['accessToken']
    generated=api.request('POST','/tokens',{'name':'Native Ygg CI','expiresInDays':1,'scopes':['*']})
    api.token=generated['token']
    os.environ['REMNAWAVE_API_TOKEN']=api.token
    keys=api.request('GET','/keygen')
    os.environ['SECRET_KEY']=keys.get('secretKey') or keys['pubKey']
    run('docker','run','-d','--name','remnanode','--network','host','-e','NODE_PORT=2222',
        '-e','SECRET_KEY','remnawave/node:latest',timeout=300)
    key=base64.urlsafe_b64encode(secrets.token_bytes(32)).decode().rstrip('=')
    raw={'tag':'VLESS_CI','listen':'::','port':443,'protocol':'vless','settings':{'clients':[],'decryption':'none'},
         'streamSettings':{'network':'tcp','security':'reality','realitySettings':{
            'show':False,'dest':'www.microsoft.com:443','xver':0,'privateKey':key,
            'serverNames':['www.microsoft.com'],'shortIds':['1234567890abcdef']}}}
    profile=api.request('POST','/config-profiles',{'name':'Native Ygg CI','config':{
        'log':{'loglevel':'warning'},'inbounds':[raw],'outbounds':[{'protocol':'freedom','tag':'DIRECT'}]}})
    inbound=profile['inbounds'][0]['uuid']
    node=api.request('POST','/nodes',{'name':'Native Ygg CI','address':'127.0.0.1','port':2222,
         'configProfile':{'activeConfigProfileUuid':profile['uuid'],'activeInbounds':[inbound]}})
    wait(lambda: next((n for n in api.nodes() if n.uuid==node['uuid']),None))
    squad=api.request('POST','/internal-squads',{'name':'Native Ygg CI','inbounds':[inbound]})
    user=api.request('POST','/users',{'username':'ygg-ci-user','expireAt':(datetime.datetime.now(datetime.timezone.utc)+datetime.timedelta(days=1)).isoformat(),
            'activeInternalSquads':[squad['uuid']]})
    source=api.request('POST','/hosts',{'inbound':{'configProfileUuid':profile['uuid'],'configProfileInboundUuid':inbound},
        'remark':'Original public','address':'127.0.0.1','port':443,'sni':'www.microsoft.com','fingerprint':'chrome',
        'nodes':[node['uuid']],'internalSquads':{'mode':'ALLOW_ONLY','squads':[squad['uuid']]}})
    # Wait until the node receives its new user, then prove the public service works first.
    time.sleep(8)
    _,original_client=export(raw,source,user); verify(original_client)
    snapshot={'profile':api.request('GET','/config-profiles/'+profile['uuid'])['config'],
              'source':api.request('GET','/hosts/'+source['uuid']),
              'containers':run('docker','inspect','--format','{{.Id}} {{.State.StartedAt}}','remnanode','test-remnawave'),
              'docker_nat':run('iptables','-t','nat','-S','DOCKER')}
    peer_config=json.loads(run(str(ROOT/'.test-bin/ygg/usr/bin/yggdrasil'),'-genconf','-json'))
    with socket.socket() as s: s.bind(('127.0.0.1',0)); peer_port=s.getsockname()[1]
    peer_config.update(IfName='none',AdminListen='none',Peers=[],MulticastInterfaces=[],Listen=[f'tcp://127.0.0.1:{peer_port}'])
    peer_file=ROOT/'.test-bin/peer.json'; peer_file.write_text(json.dumps(peer_config))
    peer_log=open(ROOT/'.test-bin/peer.log','w')
    peer=subprocess.Popen([str(ROOT/'.test-bin/ygg/usr/bin/yggdrasil'),'-useconffile',str(peer_file)],stdout=peer_log,stderr=peer_log)
    try:
        run('ufw','allow','443/tcp')
        run('ufw','--force','enable')
        # Reproduce eGames IPv6 disable, while IPv4 Remnawave remains operational.
        run('sysctl','-w','net.ipv6.conf.all.disable_ipv6=1')
        args=[sys.executable,str(ROOT/'main.py'),'--yes','--no-ssh','--panel','http://127.0.0.1:3000',
              '--node',node['uuid'],'--source-host',source['uuid'],'--name','NL Ygg CI',
              '--allow-ip','200:db8::111','--allow-ip','300:db8:1:1::/64',
              '--user-uuid',user['uuid'],'--peer',f'tcp://127.0.0.1:{peer_port}']
        subprocess.run(args,check=True)
        first=json.loads(Path('/etc/remnawave-yggdrasil/state.json').read_text())
        subprocess.run(args,check=True)
        second=json.loads(Path('/etc/remnawave-yggdrasil/state.json').read_text())
        assert first['host_uuid']==second['host_uuid']
        assert snapshot['profile']==api.request('GET','/config-profiles/'+profile['uuid'])['config']
        assert snapshot['source']==api.request('GET','/hosts/'+source['uuid'])
        assert snapshot['containers']==run('docker','inspect','--format','{{.Id}} {{.State.StartedAt}}','remnanode','test-remnawave')
        assert snapshot['docker_nat']==run('iptables','-t','nat','-S','DOCKER')
        managed=[h for h in api.hosts() if 'YGGDRASIL_NATIVE' in h.raw.get('tags',[])]
        assert len(managed)==1
        assert managed[0].raw['internalSquads']==source['internalSquads']
        _,public_client=export(raw,source,user); verify(public_client)
        assert run('sysctl','-n','net.ipv6.conf.default.disable_ipv6').strip()=='1'
        assert 'HTTP 204' in second['verification']
        print('PASS real panel/node: native IPv6 VLESS HTTPS, public VLESS preserved, no profile/source/container/Docker-rule changes, idempotent Host, scoped IPv6 enable')
        # Real remote source-IP test via network namespace; API 2222 must stay reachable.
        address=second['address']
        run('ip','netns','add','remna-ygg-ci')
        run('ip','link','add','ry-host','type','veth','peer','name','ry-client')
        run('ip','link','set','ry-client','netns','remna-ygg-ci')
        run('sysctl','-w','net.ipv6.conf.ry-host.disable_ipv6=0')
        run('ip','-6','addr','add','fd42:4567::1/64','dev','ry-host','nodad')
        run('ip','link','set','ry-host','up')
        for cidr in ('200:db8::/64','300:db8:1::/48'):
            run('ip','-6','route','add',cidr,'via','fd42:4567::2','dev','ry-host')
        ns=['ip','netns','exec','remna-ygg-ci']
        run(*ns,'sysctl','-w','net.ipv6.conf.all.disable_ipv6=0')
        run(*ns,'ip','link','set','lo','up'); run(*ns,'ip','link','set','ry-client','up')
        for ip in ('fd42:4567::2/64','200:db8::111/128','200:db8::222/128','300:db8:1:1::111/128','300:db8:1:2::111/128'):
            run(*ns,'ip','-6','addr','add',ip,'dev','ry-client','nodad')
        run(*ns,'ip','-6','route','add',address+'/128','via','fd42:4567::1','dev','ry-client')
        code='''import socket,sys
for source,allowed in [('200:db8::111',True),('200:db8::222',False),('300:db8:1:1::111',True),('300:db8:1:2::111',False)]:
 for port in [443,22]:
  with socket.socket(socket.AF_INET6,socket.SOCK_STREAM) as s:
   s.settimeout(3);s.bind((source,0))
   try:s.connect((sys.argv[1],port));connected=True
   except (TimeoutError,ConnectionRefusedError):connected=False
   assert connected==(allowed and port==443),(source,port,connected)
print('PASS remote namespace: router/LAN allowed, other clients blocked, SSH off')
'''
        print(run(*ns,sys.executable,'-c',code,address))
        run('ip','link','del','ry-host'); run('ip','netns','del','remna-ygg-ci')
    finally:
        peer.terminate();peer.wait(timeout=10);peer_log.close()

if __name__=='__main__': main()
