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
import urllib.request
import urllib.parse
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from api import API, Error
from node import run
from probe import export, verify
ROOT=Path(__file__).resolve().parents[1]


def wait(action,seconds=180):
    deadline=time.monotonic()+seconds
    last='not ready'
    while time.monotonic()<deadline:
        try:
            value=action()
            if value: return value
        except (Error,OSError) as exc: last=str(exc)
        time.sleep(2)
    raise RuntimeError('CI stack did not become ready: '+last)


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
    print('CI: real backend API ready',flush=True)
    admin=api.request('POST','/auth/register',{'username':'ciadmin','password':'Aa1'+secrets.token_hex(20)})
    api.token=admin['accessToken']
    # Only test bootstrap emulates the admin UI to mint a real API token.
    request=urllib.request.Request(api.url+'/tokens',json.dumps({'name':'Native Ygg CI','expiresInDays':1,'scopes':['*']}).encode(),
             headers={'Authorization':'Bearer '+api.token,'Content-Type':'application/json',
                      'X-Remnawave-Client-Type':'browser','X-Forwarded-Proto':'https','X-Forwarded-For':'127.0.0.1'},method='POST')
    with urllib.request.urlopen(request,timeout=20) as response: generated=json.load(response)['response']
    api.token=generated['token']
    os.environ['REMNAWAVE_API_TOKEN']=api.token
    keys=api.request('GET','/keygen')
    os.environ['SECRET_KEY']=keys.get('secretKey') or keys['pubKey']
    run('docker','run','-d','--name','remnanode','--network','host','-v','/dev/shm:/dev/shm','-e','NODE_PORT=2222',
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
    print('CI: real Remnanode connected',flush=True)
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
              '--allow-ip','200:db8::111','--allow-ip','200:db8::333','--allow-ip','300:db8:1:1::/64',
              '--user',user['username'],'--peer',f'tcp://127.0.0.1:{peer_port}']
        subprocess.run(args,check=True)
        first=json.loads(Path('/etc/remnawave-yggdrasil/state.json').read_text())
        subprocess.run(args,check=True)
        second=json.loads(Path('/etc/remnawave-yggdrasil/state.json').read_text())
        assert first['host_uuid']==second['host_uuid']
        assert first['address']==second['address']
        run('systemctl','restart','yggdrasil')
        time.sleep(2)
        verify(export(raw,dict(source,address=second['address']),user)[1])
        print('PASS ordinary Yggdrasil service restart, not just installer rerun')
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
        for ip in ('fd42:4567::2/64','200:db8::111/128','200:db8::222/128','200:db8::333/128','200:db8::444/128','300:db8:1:1::111/128','300:db8:1:2::111/128'):
            run(*ns,'ip','-6','addr','add',ip,'dev','ry-client','nodad')
        run(*ns,'ip','-6','route','add',address+'/128','via','fd42:4567::1','dev','ry-client')
        code='''import socket,sys
for source,allowed in [('200:db8::111',True),('200:db8::222',False),('200:db8::333',True),('200:db8::444',False),('300:db8:1:1::111',True),('300:db8:1:2::111',False)]:
 for port in [443,22]:
  with socket.socket(socket.AF_INET6,socket.SOCK_STREAM) as s:
   s.settimeout(3);s.bind((source,0))
   try:s.connect((sys.argv[1],port));connected=True
   except (TimeoutError,ConnectionRefusedError):connected=False
   assert connected==(allowed and port==443),(source,port,connected)
print('PASS remote namespace: router/LAN allowed, other clients blocked, SSH off')
'''
        print(run(*ns,sys.executable,'-c',code,address))
        # Same node/Host, independently grant owner SSH and leave friend VPN-only.
        ssh_args=[a if a!='--no-ssh' else '--ssh' for a in args]+['--ssh-port','2223','--ssh-allow-ip','200:db8::111','--ssh-allow-ip','200:db8::444']
        subprocess.run(ssh_args,check=True)
        code2="""import socket,sys
for source,expected in [('200:db8::111',True),('200:db8::333',False),('200:db8::444',True)]:
 with socket.socket(socket.AF_INET6,socket.SOCK_STREAM) as s:
  s.settimeout(3);s.bind((source,0))
  try:
   s.connect((sys.argv[1],2223));connected=s.recv(256).startswith(b'SSH-')
  except (TimeoutError,ConnectionRefusedError):connected=False
  assert connected==expected,(source,connected)
print('PASS independent SSH ACL: VPN friend cannot use SSH, SSH-only client can')
"""
        print(run(*ns,sys.executable,'-c',code2,address))
        keyfile=ROOT/'.test-bin/ssh-key'
        run('ssh-keygen','-q','-t','ed25519','-N','','-f',str(keyfile))
        Path('/root/.ssh').mkdir(mode=0o700,exist_ok=True)
        with open('/root/.ssh/authorized_keys','a') as authorized:
            authorized.write(keyfile.with_suffix('.pub').read_text())
        Path('/root/.ssh/authorized_keys').chmod(0o600)
        output=run(*ns,'ssh','-6','-b','200:db8::111','-p','2223','-i',str(keyfile),'-o','BatchMode=yes',
                   '-o','StrictHostKeyChecking=no','-o','UserKnownHostsFile=/dev/null',
                   '-o','ConnectTimeout=5','root@'+address,'printf REMNA_YGG_SSH_OK')
        assert 'REMNA_YGG_SSH_OK' in output
        subprocess.run(args,check=True)
        assert run('systemctl','is-active','remna-ygg-ssh',check=False).strip()!='active'
        assert json.loads(Path('/etc/remnawave-yggdrasil/state.json').read_text())['host_uuid']==first['host_uuid']
        assert snapshot['containers']==run('docker','inspect','--format','{{.Id}} {{.State.StartedAt}}','remnanode','test-remnawave')
        assert snapshot['profile']==api.request('GET','/config-profiles/'+profile['uuid'])['config']
        verify(public_client)
        print('PASS SSH key login and disable; original public VLESS and containers preserved')
        # New panel-managed mode: real profile append, independent squads, all transports.
        cert=ROOT/'.test-bin/hy2-cert.pem'; certkey=ROOT/'.test-bin/hy2-key.pem'
        run('openssl','req','-x509','-newkey','rsa:2048','-nodes','-days','1','-subj','/CN=127.0.0.1',
            '-addext','subjectAltName=DNS:127.0.0.1,IP:127.0.0.1','-keyout',str(certkey),'-out',str(cert))
        # Certbot lineage name deliberately differs from Host; no manual paths supplied.
        lineage=Path('/etc/letsencrypt/live/ci-different-name-0001'); lineage.mkdir(parents=True,mode=0o700)
        (lineage/'fullchain.pem').write_bytes(cert.read_bytes())
        (lineage/'privkey.pem').write_bytes(certkey.read_bytes())
        multi_args=[sys.executable,str(ROOT/'multi.py'),'--yes','--no-ssh','--panel','http://127.0.0.1:3000',
                    '--node',node['uuid'],'--source-host',source['uuid'],'--name','Multi CI',
                    '--protocol','hy2','--protocol','grpc','--protocol','xhttp','--protocol','ygg',
                    '--squad',squad['uuid'],'--allow-ip','200:db8::111','--allow-ip','200:db8::333',
                    '--allow-ip','300:db8:1:1::/64',
                    '--peer',f'tcp://127.0.0.1:{peer_port}']
        subprocess.run(multi_args,check=True)
        state=json.loads(Path('/etc/remnawave-yggdrasil/multi-state.json').read_text())
        assert json.loads(Path('/etc/remnawave-yggdrasil/cert-sync.json').read_text())['cert']==str(lineage/'fullchain.pem')
        actual=api.request('GET','/config-profiles/'+profile['uuid'])
        assert len(actual['config']['inbounds'])==5
        assert actual['config']['inbounds'][0]==raw
        assert api.request('GET','/hosts/'+source['uuid'])==snapshot['source']
        ygg_host=next(h for h in state['hosts'] if h['protocol']=='ygg')
        ygg_raw=next(i for i in actual['config']['inbounds'] if i['tag']==ygg_host['tag'])
        time.sleep(10)
        verify(export(ygg_raw,dict(source,address=address,port=1488),user)[1])
        verify(public_client)
        links=api.request('GET','/subscriptions/by-username/'+user['username'])['links']
        parsed=[urllib.parse.urlsplit(link) for link in links]
        names=[urllib.parse.unquote(link.fragment) for link in parsed]
        assert all(h['name'] in names for h in state['hosts'])
        # Test the same user-specific transport parameters the normal subscription exposes.
        for host in state['hosts']:
            protocol=host['protocol']
            if protocol=='ygg': continue
            link=next(link for link in parsed if urllib.parse.unquote(link.fragment)==host['name'])
            params=urllib.parse.parse_qs(link.query)
            if protocol=='hy2':
                outbound={'protocol':'hysteria','settings':{'version':2,'address':'127.0.0.1','port':host['port']},
                          'streamSettings':{'network':'hysteria','security':'tls',
                             'hysteriaSettings':{'version':2,'auth':urllib.parse.unquote(link.username)},
                             'tlsSettings':{'serverName':'127.0.0.1','alpn':['h3'],
                                'certificates':[{'certificateFile':str(cert),'usage':'verify'}]}}}
            else:
                stream={'network':protocol,'security':'reality','realitySettings':{
                    'serverName':params['sni'][0],'fingerprint':'chrome','publicKey':params['pbk'][0],
                    'shortId':params['sid'][0]}}
                if protocol=='grpc': stream['grpcSettings']={'serviceName':params['serviceName'][0]}
                if protocol=='xhttp': stream['xhttpSettings']={'path':params['path'][0],'mode':'auto'}
                outbound={'protocol':'vless','settings':{'vnext':[{'address':'127.0.0.1','port':host['port'],
                              'users':[{'id':urllib.parse.unquote(link.username),'encryption':'none'}]}]},'streamSettings':stream}
            verify(public_client,outbound_override=outbound)
            print('PASS HTTPS through subscribed '+protocol+' transport',flush=True)
        # A distinct squad with only the old inbound must not acquire any new inbound.
        other=api.request('POST','/internal-squads',{'name':'Old inbound only','inbounds':[inbound]})
        assert len(api.request('GET','/internal-squads/'+other['uuid'])['inbounds'])==1
        own=api.request('GET','/internal-squads/'+squad['uuid'])
        assert len(own['inbounds'])==5
        # An owner with only the original inbound receives none of the new Hosts.
        restricted=api.request('POST','/users',{'username':'old-inbound-only',
                   'expireAt':(datetime.datetime.now(datetime.timezone.utc)+datetime.timedelta(days=1)).isoformat(),
                   'activeInternalSquads':[other['uuid']]})
        restricted_links=api.request('GET','/subscriptions/by-username/'+restricted['username'])['links']
        restricted_names=[urllib.parse.unquote(urllib.parse.urlsplit(link).fragment) for link in restricted_links]
        assert not any(h['name'] in restricted_names for h in state['hosts'])
        print('PASS actual subscriptions: new servers appear only for permitted squads',flush=True)
        run('ip','-6','addr','add','200:db8::ffff/128','dev','lo','nodad')
        run('ip','-n','remna-ygg-ci','-6','route','add','200:db8::ffff/128','via','fd42:4567::1','dev','ry-client')
        isolation="""import socket,sys
for target,expected in [(sys.argv[1],True),('200:db8::ffff',False)]:
 with socket.socket(socket.AF_INET6,socket.SOCK_STREAM) as s:
  s.settimeout(3);s.bind(('200:db8::111',0))
  try:s.connect((target,1488));connected=True
  except (TimeoutError,ConnectionRefusedError):connected=False
  assert connected==expected,(target,connected)
print('PASS dedicated Ygg port: owner can connect only to Ygg address, other interface blocked')
"""
        print(run(*ns,sys.executable,'-c',isolation,address))
        # A manual Host edit is authoritative, including independent access restrictions.
        api.request('PATCH','/hosts',{'uuid':ygg_host['host_uuid'],'remark':'Edited in panel',
                    'internalSquads':{'mode':'ALLOW_ONLY','squads':[other['uuid']]}})
        subprocess.run(multi_args,check=True)
        edited=api.request('GET','/hosts/'+ygg_host['host_uuid'])
        assert edited['remark']=='Edited in panel' and edited['internalSquads']['squads']==[other['uuid']]
        assert len([h for h in api.hosts() if any(t.startswith('REMNA_MULTI_') for t in h.raw['tags'])])==4
        assert Path('/dev/shm/hysteria_key.pem').stat().st_mode & 0o077 == 0
        print('PASS all protocol listeners, panel profile/squad/Host control, preserved public VLESS, idempotence and protected certificate key')
        # Multi-mode regression: previously saved nonempty SSH ACL survived --no-ssh.
        multi_ssh_args=[a if a!='--no-ssh' else '--ssh' for a in multi_args]+[
            '--ssh-port','2223','--ssh-allow-ip','200:db8::111','--ssh-allow-ip','200:db8::444']
        subprocess.run(multi_ssh_args,check=True)
        print(run(*ns,sys.executable,'-c',code2,address))
        subprocess.run(multi_args,check=True)
        disabled_state=json.loads(Path('/etc/remnawave-yggdrasil/multi-state.json').read_text())
        assert disabled_state['ssh'] is False and disabled_state['ssh_allowed']==[]
        assert run('systemctl','is-active','remna-ygg-ssh',check=False).strip()!='active'
        # Adding only one protocol must retain the complete installed Host inventory.
        partial_args=multi_args[:]
        for protocol in ('hy2','grpc','xhttp'):
            index=partial_args.index(protocol); del partial_args[index-1:index+1]
        subprocess.run(partial_args,check=True)
        assert len(json.loads(Path('/etc/remnawave-yggdrasil/multi-state.json').read_text())['hosts'])==4
        print('PASS multi-mode SSH disable and complete Host inventory after partial rerun',flush=True)
        run('ip','link','del','ry-host'); run('ip','netns','del','remna-ygg-ci')
    finally:
        peer.terminate();peer.wait(timeout=10);peer_log.close()

if __name__=='__main__': main()
