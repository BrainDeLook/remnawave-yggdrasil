"""Optional end-to-end test and user-specific export. Uses the panel user's UUID."""
import base64
import json
import os
from pathlib import Path
import socket
import subprocess
import tempfile
import time
import urllib.parse
import zipfile
from api import Error, obj, string
from node import APP, download, run

XRAY = {'amd64':('Xray-linux-64.zip','0eec3ed4163c273d8c8bf821f7d219e257921cf584aa3fb970715e458e631978'),
        'arm64':('Xray-linux-arm64-v8a.zip','d1304946c486d98e6b886e9b931eaaaebb23835b30e217638037b7f2721d4e9c')}

def export(raw: dict, payload: dict, user: dict) -> tuple[str,dict]:
    user = obj(user)
    if user.get('status') != 'ACTIVE':
        raise Error('Для проверки нужен активный пользователь панели')
    identifier = string(user,'vlessUuid')
    settings = raw['streamSettings']['realitySettings']
    key = settings['privateKey']
    try:
        private = base64.urlsafe_b64decode(key+'='*((-len(key))%4))
    except ValueError:
        raise Error('Неверный формат X25519 privateKey') from None
    if len(private) != 32:
        raise Error('Неподдерживаемый Reality privateKey')
    public = subprocess.run(['openssl','pkey','-inform','DER','-pubout','-outform','DER'],
             input=bytes.fromhex('302e020100300506032b656e04220420')+private,
             capture_output=True,check=True).stdout[-32:]
    public_key = base64.urlsafe_b64encode(public).decode().rstrip('=')
    short_id = settings.get('shortIds',[''])[0]
    fingerprint = payload.get('fingerprint') or 'chrome'
    # Remnanode assigns Vision flow to TCP/RAW Reality users. Verify this with a real user.
    out = {'type':'vless','tag':payload['remark'],'server':payload['address'],'server_port':payload['port'],
           'uuid':identifier,'flow':'xtls-rprx-vision',
           'tls':{'enabled':True,'server_name':payload['sni'],
                  'utls':{'enabled':True,'fingerprint':fingerprint},
                  'reality':{'enabled':True,'public_key':public_key,'short_id':short_id}}}
    query=urllib.parse.urlencode({'encryption':'none','security':'reality','type':'tcp','flow':'xtls-rprx-vision',
          'sni':payload['sni'],'fp':fingerprint,'pbk':public_key,'sid':short_id})
    link=f"vless://{identifier}@[{payload['address']}]:{payload['port']}?{query}#{urllib.parse.quote(payload['remark'],safe='')}"
    return link,out


def verify(out: dict, outbound_override: dict | None = None):
    binary = os.environ.get('REMNA_YGG_XRAY')
    if not binary:
        arch=run('dpkg','--print-architecture').strip()
        name,digest=XRAY[arch]
        with tempfile.TemporaryDirectory(prefix='remna-xray-') as directory:
            archive=Path(directory)/'xray.zip'
            download('https://github.com/XTLS/Xray-core/releases/download/v26.10.21/'+name,archive,digest)
            with zipfile.ZipFile(archive) as bundle:
                content=bundle.read('xray')
            binary=str(APP/'check-xray')
            Path(binary).write_bytes(content)
            Path(binary).chmod(0o700)
    with socket.socket() as listener:
        listener.bind(('127.0.0.1',0)); port=listener.getsockname()[1]
    tls=out['tls']
    config={'log':{'loglevel':'warning'},'inbounds':[{'listen':'127.0.0.1','port':port,'protocol':'socks','settings':{'auth':'noauth'}}],
            'outbounds':[{'protocol':'vless','settings':{'vnext':[{'address':out['server'],'port':out['server_port'],
                         'users':[{'id':out['uuid'],'encryption':'none','flow':out['flow']}]}]},
            'streamSettings':{'network':'tcp','security':'reality','realitySettings':{
                         'serverName':tls['server_name'],'fingerprint':tls['utls']['fingerprint'],
                         'publicKey':tls['reality']['public_key'],'shortId':tls['reality']['short_id']}}}]}
    if outbound_override is not None: config['outbounds']=[outbound_override]
    with tempfile.TemporaryDirectory(prefix='remna-ygg-check-') as directory:
        path=Path(directory)/'client.json'; path.write_text(json.dumps(config)); path.chmod(0o600)
        with open(Path(directory)/'client.log','w') as log:
            process=subprocess.Popen([binary,'run','-c',str(path)],stdout=log,stderr=log)
            try:
                for _ in range(40):
                    if process.poll() is not None:
                        raise Error('Проверочный Xray завершился до подключения')
                    with socket.socket() as check:
                        if check.connect_ex(('127.0.0.1',port))==0: break
                    time.sleep(.1)
                code=run('curl','--silent','--show-error','--noproxy','','--proxy',f'socks5h://127.0.0.1:{port}',
                         '--connect-timeout','15','--max-time','35','--output','/dev/null',
                         '--write-out','%{http_code}','https://www.gstatic.com/generate_204',timeout=45)
                if code.strip()!='204':
                    raise Error('VLESS проверка не получила HTTP 204')
            finally:
                process.terminate()
                process.wait(timeout=10)
