#!/usr/bin/env python3
"""Read-only terminal compatibility report; no panel, VPN or SSH changes."""
import os
import re
import select
import termios
import time

print('RemnaYgg · terminal compatibility report',flush=True)
for name in ('TERM','COLORTERM','TERM_PROGRAM'):
    print(name+'='+repr(os.environ.get(name,'')),flush=True)
descriptor=os.open('/dev/tty',os.O_RDWR)
original=termios.tcgetattr(descriptor)
settings=termios.tcgetattr(descriptor)
settings[3]&=~(termios.ECHO|termios.ICANON)
settings[6][termios.VMIN]=0; settings[6][termios.VTIME]=0
queries=[('palette-BEL',b'\x1b]4;255;?\x07'),('palette-ST',b'\x1b]4;255;?\x1b\\'),
         ('colors',b'\x1bP+q436f\x1b\\'),('version',b'\x1b[>0q'),('device',b'\x1b[>0c')]
results=[]
try:
    termios.tcsetattr(descriptor,termios.TCSANOW,settings)
    for name,query in queries:
        os.write(descriptor,query); data=b''; deadline=time.monotonic()+1
        while time.monotonic()<deadline and len(data)<4096:
            if select.select([descriptor],[],[],max(0,deadline-time.monotonic()))[0]:
                part=os.read(descriptor,4096-len(data))
                if not part: break
                data+=part
        # Print only protocol replies, never ordinary text entered during the query.
        replies=re.findall(rb'\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\)|P[^\x1b]*\x1b\\)',data)
        results.append((name,replies))
finally:
    termios.tcsetattr(descriptor,termios.TCSANOW,original)
    os.close(descriptor)
for name,replies in results:
    print(name+': '+(', '.join(repr(value) for value in replies) or 'no reply'))
