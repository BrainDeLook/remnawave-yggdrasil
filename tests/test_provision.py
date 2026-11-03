import copy
from pathlib import Path
import tempfile
import unittest
from uuid import uuid4
from api import Host, Node
from converter import Profile, convert
from provision import apply
from test_api import NODE, SOURCE
from test_converter import BASE


class Panel:
    def __init__(self,config,granted):
        self.config=config
        self.ids={i['tag']:str(uuid4()) for i in config['inbounds']}
        self.created=[]; self.updates=[]; self.granted=granted
    def nodes(self): return [Node.parse(NODE)]
    def hosts(self): return []
    def request(self,method,path,body=None):
        if method=='GET' and path.startswith('/nodes/'):
            return dict(NODE,isConnected=False)  # Expected transient while applying the profile.
        if method=='GET' and path.startswith('/config-profiles/'):
            return {'config':copy.deepcopy(self.config),'inbounds':[{'uuid':value,'tag':tag} for tag,value in self.ids.items()]}
        if method=='PATCH' and path=='/nodes': self.updates.append(body); return {}
        if method=='GET' and path.startswith('/internal-squads/'):
            return {'inbounds':[{'uuid':i} for i in self.granted]}
        if method=='PATCH' and path=='/internal-squads': self.granted=body['inbounds']; return {}
        if method=='POST' and path=='/hosts':
            self.created.append(body); return dict(body,uuid=str(uuid4()))
        raise AssertionError((method,path))


class ProvisionTests(unittest.TestCase):
    def test_unassigned_host_disabled_and_not_granted_to_everyone(self):
        config,mapping=convert(Profile.parse({'inbounds':[BASE]}),'node-tcp',['ygg'])
        panel=Panel(config,[])
        with tempfile.TemporaryDirectory() as directory:
            apply(panel,Node.parse(NODE),config,config,mapping,Host.parse(SOURCE),'200:db8::1','Node',{'ygg':[]},Path(directory))
        self.assertTrue(panel.created[0]['isDisabled'])
        self.assertEqual(panel.created[0]['internalSquads'],{'mode':'EXCLUDE','squads':[]})
        self.assertEqual(panel.granted,[])
    def test_squad_addition_preserves_old_inbounds_and_limits_host(self):
        config,mapping=convert(Profile.parse({'inbounds':[BASE]}),'node-tcp',['ygg'])
        old=str(uuid4()); squad=str(uuid4()); panel=Panel(config,[old])
        with tempfile.TemporaryDirectory() as directory:
            apply(panel,Node.parse(NODE),config,config,mapping,Host.parse(SOURCE),'200:db8::1','Node',{'ygg':[squad]},Path(directory))
        self.assertEqual(panel.granted,[old,panel.ids[mapping['ygg']]])
        self.assertFalse(panel.created[0]['isDisabled'])
        self.assertEqual(panel.created[0]['internalSquads'],{'mode':'ALLOW_ONLY','squads':[squad]})

if __name__=='__main__': unittest.main()
