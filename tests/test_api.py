import copy
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from uuid import uuid4
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from api import API, Error, Host, Node, eligible, host_payload, upsert
from node import networks

N,P,I,H=map(str,[uuid4(),uuid4(),uuid4(),uuid4()])
NODE={'uuid':N,'name':'NL node','address':'node.example.com','isConnected':True,'isDisabled':False,
      'configProfile':{'activeConfigProfileUuid':P,'activeInbounds':[{'uuid':I,'tag':'REALITY'}]}}
RAW={'tag':'REALITY','port':443,'protocol':'vless','settings':{'clients':[],'decryption':'none'},
     'streamSettings':{'network':'tcp','security':'reality',
       'realitySettings':{'privateKey':'secret-never-logged','shortIds':['1234'],'serverNames':['reality.example.com']}}}
SOURCE={'uuid':H,'remark':'Original','address':'public.example.com','port':443,'inbound':{'configProfileUuid':P,'configProfileInboundUuid':I},
        'nodes':[N],'sni':'reality.example.com','fingerprint':'chrome','isDisabled':False,'isHidden':False,
        'tags':[],'overrideSniFromAddress':False,'internalSquads':{'mode':'ALLOW_ONLY','squads':[str(uuid4())]}}
PROFILE={'uuid':P,'config':{'inbounds':[RAW],'outbounds':[{'protocol':'freedom'}]},'inbounds':[{'uuid':I,'tag':'REALITY'}]}

class Handler(BaseHTTPRequestHandler):
    def log_message(self,*args): pass
    def response(self,data,status=200):
        self.send_response(status); self.send_header('Content-Type','application/json'); self.end_headers(); self.wfile.write(json.dumps({'response':data}).encode())
    def do_GET(self):
        self.server.reads.append(self.path)
        if self.path=='/api/redirect':
            self.send_response(302); self.send_header('Location','http://127.0.0.1:9/credential-sink'); self.end_headers(); return
        if self.headers.get('Authorization')!='Bearer TEST_TOKEN': self.response({},401); return
        data={'/api/nodes':[NODE],'/api/hosts':self.server.hosts,'/api/config-profiles/'+P:PROFILE}
        self.response(data[self.path])
    def write_host(self,method):
        body=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        self.server.writes.append((method,self.path,body))
        if method=='PATCH':
            host=next(h for h in self.server.hosts if h['uuid']==body['uuid']); host.update(body)
        else:
            host=dict(body,uuid=str(uuid4())); self.server.hosts.append(host)
        self.response(host)
    def do_POST(self): self.write_host('POST')
    def do_PATCH(self): self.write_host('PATCH')

class Integration(unittest.TestCase):
    def setUp(self):
        self.server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        self.server.hosts=[copy.deepcopy(SOURCE)]; self.server.writes=[]; self.server.reads=[]
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True); self.thread.start()
        self.api=API('http://127.0.0.1:'+str(self.server.server_port),'TEST_TOKEN')
    def tearDown(self): self.server.shutdown(); self.server.server_close(); self.thread.join()
    def test_create_repeat_update_only_owned_host_preserves_profile_source_and_squads(self):
        before=copy.deepcopy((PROFILE,self.server.hosts[0]))
        node=self.api.nodes()[0]; source=self.api.hosts()[0]
        self.assertTrue(eligible(node,source))
        raw=self.api.inbound(node,source)
        payload=host_payload(node,source,raw,'200:db8::1','Нидерланды Ygg')
        endpoint=upsert(self.api,payload,self.api.hosts(),None)
        again=upsert(self.api,payload,self.api.hosts(),endpoint.uuid)
        self.assertEqual(endpoint.uuid,again.uuid); self.assertEqual(len(self.server.writes),1)
        payload['remark']='Renamed'; upsert(self.api,payload,self.api.hosts(),endpoint.uuid)
        self.assertEqual([w[0] for w in self.server.writes],['POST','PATCH'])
        self.assertEqual((PROFILE,self.server.hosts[0]),before)
        self.assertEqual(endpoint.raw['internalSquads'],SOURCE['internalSquads'])
        self.assertEqual(endpoint.raw['nodes'],[N]); self.assertEqual(endpoint.raw['sni'],'reality.example.com')
    def test_resume_after_lost_post_response_does_not_duplicate(self):
        n=self.api.nodes()[0]; s=self.api.hosts()[0]; payload=host_payload(n,s,self.api.inbound(n,s),'200:db8::1','Ygg')
        first=upsert(self.api,payload,self.api.hosts(),None)
        second=upsert(self.api,payload,self.api.hosts(),None)
        self.assertEqual(first.uuid,second.uuid); self.assertEqual(len(self.server.writes),1)
    def test_saved_uuid_cannot_overwrite_foreign_host(self):
        n=self.api.nodes()[0]; s=self.api.hosts()[0]
        with self.assertRaises(Error): upsert(self.api,host_payload(n,s,RAW,'200:db8::1','Ygg'),[s],H)
        self.assertFalse(self.server.writes)
    def test_token_not_forwarded_on_redirect(self):
        with self.assertRaisesRegex(Error,'HTTP 302'): self.api.request('GET','/redirect')
        self.assertEqual(self.server.reads,['/api/redirect'])
    def test_sni_is_never_replaced_by_ygg_ipv6(self):
        source=copy.deepcopy(SOURCE); source.update(overrideSniFromAddress=True,address='reality.example.com')
        payload=host_payload(Node.parse(NODE),Host.parse(source),RAW,'200:db8::1','Ygg')
        self.assertEqual(payload['sni'],'reality.example.com'); self.assertFalse(payload['overrideSniFromAddress'])
    def test_unsupported_inbound_stops_before_writes(self):
        raw=PROFILE['config']['inbounds'][0]
        original=raw['streamSettings']['network']; raw['streamSettings']['network']='grpc'
        try:
            with self.assertRaises(Error): self.api.inbound(self.api.nodes()[0],self.api.hosts()[0])
        finally: raw['streamSettings']['network']=original
        self.assertFalse(self.server.writes)
    def test_no_plaintext_remote_panel(self):
        with self.assertRaises(Error): API('http://example.com','TOKEN')
    def test_disabled_and_hidden_source_cannot_be_selected(self):
        src=copy.deepcopy(SOURCE); src['isDisabled']=True
        self.assertFalse(eligible(Node.parse(NODE),Host.parse(src)))
    def test_acl_pair_and_independent_ssh(self):
        good=['200:db8::1','300:db8:1:1::/64','200:db8::2','300:db8:2:2::/64']
        self.assertEqual(networks(good,pair=True),sorted(good))
        self.assertEqual(networks(['300:db8:1:1::123']),['300:db8:1:1::123'])
        for invalid in (['200::/7'],['300::/8'],['200:db8::1'],['300:db8:1:1::/64']):
            with self.assertRaises(Error): networks(invalid,pair=True)
        with self.assertRaises(Error): networks(['300:db8:1:1::/64'],role='router')

if __name__=='__main__': unittest.main()
