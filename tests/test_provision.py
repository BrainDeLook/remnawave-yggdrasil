import copy
from pathlib import Path
import tempfile
import unittest
from uuid import uuid4
from api import Error, Host, Node
from converter import Profile, convert, LABELS, HOST_SUFFIXES
from provision import apply
from test_api import NODE, SOURCE
from test_converter import BASE


class Panel:
    def __init__(self,config,granted):
        self.config=config
        self.ids={i['tag']:str(uuid4()) for i in config['inbounds']}
        self.created=[]; self.updates=[]; self.granted=granted
        self.current_hosts=[]
        self.node=copy.deepcopy(NODE); self.requests=[]
    def nodes(self): return [Node.parse(NODE)]
    def hosts(self): return [Host.parse(h) for h in self.current_hosts]
    def request(self,method,path,body=None):
        self.requests.append((method,path,body))
        if method=='GET' and path.startswith('/nodes/'):
            return dict(self.node,isConnected=False)  # Expected transient while applying the profile.
        if method=='GET' and path.startswith('/config-profiles/'):
            return {'config':copy.deepcopy(self.config),'inbounds':[{'uuid':value,'tag':tag} for tag,value in self.ids.items()]}
        if method=='PATCH' and path=='/nodes': self.updates.append(body); return {}
        if method=='POST' and path=='/hosts':
            self.created.append(body); return dict(body,uuid=str(uuid4()))
        if method=='PATCH' and path=='/hosts':
            host=next(h for h in self.current_hosts if h['uuid']==body['uuid'])
            host.update(body); return copy.deepcopy(host)
        raise AssertionError((method,path))


class ProvisionTests(unittest.TestCase):
    def test_short_uniform_names_for_all_protocols(self):
        config,mapping=convert(Profile.parse({'inbounds':[BASE]}),'node-tcp',list(LABELS))
        panel=Panel(config,[])
        with tempfile.TemporaryDirectory() as directory:
            apply(panel,Node.parse(NODE),config,config,mapping,Host.parse(SOURCE),'200:db8::1','🇳🇱 Нидерланды',
                  Path(directory))
        self.assertEqual([h['remark'] for h in panel.created],['🇳🇱 Нидерланды '+v for v in HOST_SUFFIXES.values()])
    def test_only_generated_old_names_are_migrated_and_other_fields_preserved(self):
        for remark,expected in [('Old name · VLESS Reality Yggdrasil','New name Ygg'),
                                ('Old name Ygg','New name Ygg'),('My manual name','My manual name')]:
            config,mapping=convert(Profile.parse({'inbounds':[BASE]}),'node-tcp',['ygg'])
            panel=Panel(config,[])
            host=copy.deepcopy(SOURCE)
            host.update(remark=remark,tags=['REMNA_MULTI_YGG'],isDisabled=True,
                        internalSquads={'mode':'ALLOW_ONLY','squads':[str(uuid4())]})
            host['inbound']['configProfileInboundUuid']=panel.ids[mapping['ygg']]
            panel.current_hosts=[host]
            before=copy.deepcopy(host)
            with tempfile.TemporaryDirectory() as directory:
                apply(panel,Node.parse(NODE),config,config,mapping,Host.parse(SOURCE),'200:db8::1','New name',
                      Path(directory),previous_name='Old name')
            self.assertEqual(host,dict(before,remark=expected))
            writes=[body for method,path,body in panel.requests if method=='PATCH' and path=='/hosts']
            self.assertEqual(writes,[{'uuid':host['uuid'],'remark':expected}] if expected!=remark else [])
            self.assertEqual(panel.created,[])
    def test_node_moved_to_another_profile_stops_before_panel_writes(self):
        config,mapping=convert(Profile.parse({'inbounds':[BASE]}),'node-tcp',['ygg'])
        panel=Panel(config,[])
        panel.node['configProfile']['activeConfigProfileUuid']=str(uuid4())
        with tempfile.TemporaryDirectory() as directory:
            backup=Path(directory)/'backup'
            with self.assertRaisesRegex(Error,'профиль ноды изменился'):
                apply(panel,Node.parse(NODE),config,config,mapping,Host.parse(SOURCE),'200:db8::1','Node',backup)
            self.assertFalse(backup.exists())
        self.assertFalse([r for r in panel.requests if r[0]!='GET'])
    def test_concurrent_profile_edit_stops_before_panel_writes(self):
        before={'inbounds':[BASE]}
        config,mapping=convert(Profile.parse(before),'node-tcp',['ygg'])
        panel=Panel(dict(before,log={'loglevel':'debug'}),[])
        with tempfile.TemporaryDirectory() as directory:
            backup=Path(directory)/'backup'
            with self.assertRaisesRegex(Error,'Профиль изменился'):
                apply(panel,Node.parse(NODE),before,config,mapping,Host.parse(SOURCE),'200:db8::1','Node',backup)
            self.assertFalse(backup.exists())
        self.assertFalse([r for r in panel.requests if r[0]!='GET'])
    def test_existing_transport_uses_normal_defaults_when_settings_omitted(self):
        for protocol,expected in [('grpc',''),('xhttp','/')]:
            config,mapping=convert(Profile.parse({'inbounds':[BASE]}),'node-tcp',[protocol])
            config['inbounds'][1]['streamSettings'].pop(protocol+'Settings')
            panel=Panel(config,[])
            with tempfile.TemporaryDirectory() as directory:
                apply(panel,Node.parse(NODE),config,config,mapping,Host.parse(SOURCE),'200:db8::1','Node',Path(directory))
            self.assertEqual(panel.created[0]['path'],expected)
    def test_new_host_enabled_without_squad_filter_or_automatic_grants(self):
        config,mapping=convert(Profile.parse({'inbounds':[BASE]}),'node-tcp',['ygg'])
        panel=Panel(config,[])
        with tempfile.TemporaryDirectory() as directory:
            apply(panel,Node.parse(NODE),config,config,mapping,Host.parse(SOURCE),'200:db8::1','Node',Path(directory))
        self.assertFalse(panel.created[0]['isDisabled'])
        self.assertEqual(panel.created[0]['internalSquads'],{'mode':'EXCLUDE','squads':[]})
        self.assertEqual(panel.granted,[])
    def test_existing_squad_permissions_unchanged(self):
        config,mapping=convert(Profile.parse({'inbounds':[BASE]}),'node-tcp',['ygg'])
        old=str(uuid4()); panel=Panel(config,[old])
        with tempfile.TemporaryDirectory() as directory:
            apply(panel,Node.parse(NODE),config,config,mapping,Host.parse(SOURCE),'200:db8::1','Node',Path(directory))
        self.assertEqual(panel.granted,[old])
        self.assertFalse(panel.created[0]['isDisabled'])
        self.assertEqual(panel.created[0]['internalSquads'],{'mode':'EXCLUDE','squads':[]})

if __name__=='__main__': unittest.main()
