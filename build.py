"""Deterministic release bundle. --update pins the bootstrap after source changes."""
import argparse
import hashlib
import io
from pathlib import Path
import re
import tarfile
root=Path(__file__).resolve().parent
parser=argparse.ArgumentParser(); parser.add_argument('--update',action='store_true'); args=parser.parse_args()
version='v0.2.0'
buffer=io.BytesIO()
with tarfile.open(fileobj=buffer,mode='w',format=tarfile.USTAR_FORMAT) as bundle:
    for name in ('api.py','main.py','node.py','probe.py','multi.py','converter.py','provision.py','certificates.py'):
        data=(root/name).read_bytes(); entry=tarfile.TarInfo(name)
        entry.size=len(data); entry.mode=0o700; entry.mtime=0
        bundle.addfile(entry,io.BytesIO(data))
data=buffer.getvalue(); digest=hashlib.sha256(data).hexdigest()
(root/'dist').mkdir(exist_ok=True)
archive=root/'dist'/f'remnawave-yggdrasil-{version}.tar'; archive.write_bytes(data)
installer=root/'install.sh'; text=installer.read_text(encoding='utf-8')
if args.update:
    installer.write_text(re.sub(r'BUNDLE_SHA256=\w+','BUNDLE_SHA256='+digest,text),encoding='utf-8',newline='\n')
else:
    assert 'BUNDLE_SHA256='+digest in text, 'Run python build.py --update before committing'
sums=''.join(hashlib.sha256(p.read_bytes()).hexdigest()+'  '+p.name+'\n' for p in (archive,installer))
if args.update: (root/'SHA256SUMS').write_text(sums,encoding='utf-8',newline='\n')
else: assert (root/'SHA256SUMS').read_text()==sums
print('PASS deterministic bundle, bootstrap hash and SHA256SUMS')
