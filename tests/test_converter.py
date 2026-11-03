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
    def test_new_transports_inherit_source_specific_routing(self):
        self.original['routing']['rules'] += [
            {'inboundTag':['node-tcp'],'domain':['blocked.example.com'],'outboundTag':'BLOCK'},
            {'inboundTag':['other-listener'],'outboundTag':'DIRECT'},
            {'inboundTag':['node-tcp','other-listener'],'balancerTag':'CHAIN'},
        ]
        snapshot=copy.deepcopy(self.original)
        result,mapping=convert(Profile.parse(self.original),'node-tcp',['hy2','grpc','xhttp','ygg'])
        tags=list(mapping.values())
        self.assertEqual(result['routing']['rules'][1]['inboundTag'],['node-tcp']+tags)
        self.assertEqual(result['routing']['rules'][2],snapshot['routing']['rules'][2])
        self.assertEqual(result['routing']['rules'][3]['inboundTag'],['node-tcp','other-listener']+tags)
        self.assertEqual(self.original,snapshot)
        again,_=convert(Profile.parse(result),'node-tcp',['hy2','grpc','xhttp','ygg'])
        self.assertEqual(again,result)
    def test_existing_transport_keeps_its_separate_routing(self):
        config,_=convert(Profile.parse(self.original),'node-tcp',['grpc'])
        config['routing']['rules'].append({'inboundTag':['node-tcp'],'outboundTag':'CHAIN'})
        result,mapping=convert(Profile.parse(config),'node-tcp',['grpc','ygg'])
        self.assertEqual(result['routing']['rules'][-1]['inboundTag'],['node-tcp',mapping['ygg']])
    def test_vision_does_not_carry_over_to_grpc_or_xhttp(self):
        self.original['inbounds'][0]['settings']['flow']='xtls-rprx-vision'
        result,mapping=convert(Profile.parse(self.original),'node-tcp',['grpc','xhttp','ygg'])
        for protocol in ('grpc','xhttp'):
            inbound=next(i for i in result['inbounds'] if i['tag']==mapping[protocol])
            self.assertNotIn('flow',inbound['settings'])
        ygg=next(i for i in result['inbounds'] if i['tag']==mapping['ygg'])
        self.assertEqual(ygg['settings']['flow'],'xtls-rprx-vision')
        self.assertEqual(result['inbounds'][0]['settings']['flow'],'xtls-rprx-vision')
    def test_new_hysteria_uses_its_own_certificate_files(self):
        result,mapping=convert(Profile.parse(self.original),'node-tcp',['hy2'])
        hy2=next(i for i in result['inbounds'] if i['tag']==mapping['hy2'])
        self.assertEqual(hy2['streamSettings']['tlsSettings']['certificates'],[{
            'certificateFile':'/dev/shm/remna-ygg-hy2-cert.pem',
            'keyFile':'/dev/shm/remna-ygg-hy2-key.pem'}])
    def test_reserved_port_collision_is_rejected(self):
        with self.assertRaisesRegex(ConversionError,'уже занят'): convert(Profile.parse(self.original),'node-tcp',['ygg'],{'ygg':443})
    def test_invalid_profiles_rejected_at_boundary(self):
        for data in ([],{}, {'inbounds':[{}]}, {'inbounds':[BASE,BASE]}):
            with self.assertRaises(ConversionError): Profile.parse(data)
        for routing in ([], {'rules':{}}, {'rules':[None]}, {'rules':[{'inboundTag':'node-tcp'}]}):
            with self.assertRaises(ConversionError): Profile.parse(dict(self.original,routing=routing))
    def test_selection_is_optional_independent(self):
        for protocol in ('hy2','grpc','xhttp','ygg'):
            result,mapping=convert(Profile.parse(self.original),'node-tcp',[protocol])
            self.assertEqual(len(result['inbounds']),2); self.assertEqual(list(mapping),[protocol])

if __name__=='__main__': unittest.main()
