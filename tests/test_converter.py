import copy
import json
from pathlib import Path
import sys
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from converter import Profile, convert, ConversionError

BASE={'tag':'node-tcp','listen':'0.0.0.0','port':443,'protocol':'vless',
      'settings':{'clients':[],'decryption':'none'},'streamSettings':{'network':'tcp','security':'reality',
      'realitySettings':{'dest':'/dev/shm/nginx.sock','xver':1,'privateKey':'fixture-only',
                         'serverNames':['node.example.com'],'shortIds':['1234']}}}

class ConverterTests(unittest.TestCase):
    def setUp(self):
        self.original={'log':{'loglevel':'warning'},'inbounds':[copy.deepcopy(BASE)],
                       'dns':{'queryStrategy':'UseIPv4'},'routing':{'rules':[{'ip':['geoip:private'],'outboundTag':'BLOCK'}]}}
    def test_all_protocols_preserve_original_and_actual_reality_target(self):
        snapshot=copy.deepcopy(self.original)
        result,mapping=convert(Profile.parse(self.original),'node-tcp',['hy2','grpc','xhttp','ygg'])
        self.assertEqual(self.original,snapshot)
        self.assertEqual(result['inbounds'][0],snapshot['inbounds'][0])
        self.assertEqual(result['routing'],snapshot['routing'])
        self.assertEqual(result['dns'],snapshot['dns'])
        for i in result['inbounds'][2:]: self.assertEqual(i['streamSettings']['realitySettings'],BASE['streamSettings']['realitySettings'])
        ygg=next(i for i in result['inbounds'] if i['tag']==mapping['ygg'])
        self.assertEqual((ygg['listen'],ygg['port']),('::',1488))
    def test_rerun_preserves_panel_changes_and_does_not_duplicate(self):
        result,mapping=convert(Profile.parse(self.original),'node-tcp',['ygg'])
        result['inbounds'][1]['port']=1588
        result['inbounds'][1]['streamSettings']['realitySettings']['serverNames']=['edited.example.com']
        again,_=convert(Profile.parse(result),'node-tcp',['ygg'])
        self.assertEqual(result,again)
    def test_existing_upstream_transports_reused_without_duplication(self):
        result,mapping=convert(Profile.parse(self.original),'node-tcp',['grpc','xhttp'])
        result['inbounds'][1]['tag']='existing-grpc-8443'
        again,mapping=convert(Profile.parse(result),'node-tcp',['grpc','xhttp','ygg'])
        self.assertEqual(mapping['grpc'],'existing-grpc-8443')
        self.assertEqual(len(again['inbounds']),4)
    def test_reserved_port_collision_is_rejected(self):
        with self.assertRaisesRegex(ConversionError,'уже занят'): convert(Profile.parse(self.original),'node-tcp',['ygg'],{'ygg':443})
    def test_invalid_profiles_rejected_at_boundary(self):
        for data in ([],{}, {'inbounds':[{}]}, {'inbounds':[BASE,BASE]}):
            with self.assertRaises(ConversionError): Profile.parse(data)
    def test_selection_is_optional_independent(self):
        for protocol in ('hy2','grpc','xhttp','ygg'):
            result,mapping=convert(Profile.parse(self.original),'node-tcp',[protocol])
            self.assertEqual(len(result['inbounds']),2); self.assertEqual(list(mapping),[protocol])

if __name__=='__main__': unittest.main()
