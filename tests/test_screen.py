import contextlib
import io
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from screen import Answer,Back,Screen,available,clip,width,wrap
import node
import ui


class Window:
    def __init__(self,keys=(),size=(24,80)):
        self.keys=iter(keys); self.size=size; self.written=[]
    def getmaxyx(self): return self.size
    def erase(self): pass
    def clear(self): pass
    def refresh(self): pass
    def move(self,row,col):
        assert 0<=row<self.size[0] and 0<=col<self.size[1]
    def get_wch(self): return next(self.keys)
    def addstr(self,row,col,text,*style):
        assert 0<=row<self.size[0] and col+width(text)<self.size[1]
        self.written.append(text)


def terminal(window):
    constants={name:100+i for i,name in enumerate(('KEY_LEFT','KEY_RIGHT','KEY_UP','KEY_DOWN',
        'KEY_PPAGE','KEY_NPAGE','KEY_HOME','KEY_END','KEY_DC','KEY_BACKSPACE','KEY_ENTER','KEY_RESIZE'))}
    return Screen(window,SimpleNamespace(**constants,A_REVERSE=0,has_colors=lambda:False,curs_set=lambda n:None),'test')


class ScreenTests(unittest.TestCase):
    def test_plain_and_automated_modes_never_open_a_terminal(self):
        with patch('sys.stdin.isatty',return_value=True),patch('sys.stdout.isatty',return_value=True),patch.dict(os.environ,TERM='xterm'):
            self.assertTrue(available([]))
            for flag in ('--plain','--yes','--help','--check','--result','--network-only','--sync-certs'):
                self.assertFalse(available([flag]))
        with patch('sys.stdin.isatty',return_value=False): self.assertFalse(available([]))

    def test_back_replays_read_only_answers_by_stable_identity(self):
        app=terminal(Window())
        app.answers=[Answer(('select','Нода',('a','b')),'b')]
        app.replay_until=1
        self.assertEqual(app.select('Нода',[('b','Renamed B'),('a','A')],'a'),'b')

    def test_editing_earlier_answer_discards_dependent_secrets(self):
        app=terminal(Window())
        app.answers=[Answer(('input','Panel'),'old'),Answer(('secret','Token'),'private')]
        self.assertEqual(app.remember(('input','Panel'),'old',lambda previous:'new'),'new')
        self.assertEqual(app.answers,[Answer(('input','Panel'),'new')])

    def test_secret_is_never_rendered_even_when_revisiting(self):
        token='UNIQUE_PRIVATE_TOKEN'
        window=Window([*token,'\n','\n']); app=terminal(window)
        self.assertEqual(app.prompt('Токен',secret=True),token)
        app.position=0
        self.assertEqual(app.prompt('Токен',secret=True),token)
        self.assertNotIn(token,'\n'.join(window.written))

    def test_default_confirmation_is_no_in_screen_mode(self):
        app=terminal(Window(['\n']))
        with patch.object(ui,'screen',app): self.assertFalse(ui.confirm('Применить план?'))

    def test_multiselect_requires_a_selection_only_when_requested(self):
        window=Window(['\n',' ','\n']); app=terminal(window)
        self.assertEqual(app.select('Дополнения',[('ygg','Ygg')],[],True,False),['ygg'])
        self.assertIn('Отметьте хотя бы одно дополнение пробелом.','\n'.join(window.written))
        self.assertEqual(app.select('Сквады',[],[],True,True),[])

    def test_apply_phase_cannot_go_back_or_replay(self):
        app=terminal(Window(['\x1b'])); app.applying=True
        self.assertIsNone(app.key())

    def test_unicode_and_long_lines_fit_without_losing_text(self):
        value='Русский текст 中文 и длинный IPv6 200:1111:2222:3333:4444:5555:6666:7777'
        self.assertLessEqual(width(clip(value,30)),30)
        self.assertEqual(''.join(wrap(value.replace(' ',''),20)),value.replace(' ',''))
        self.assertTrue(all(width(line)<=20 for line in wrap(value,20)))

    def test_small_terminal_keeps_input_until_resize(self):
        window=Window(size=(10,40)); app=terminal(window)
        keys=iter(['я',app.curses.KEY_RESIZE,'\n'])
        def read():
            key=next(keys)
            if key==app.curses.KEY_RESIZE: window.size=(24,80)
            return key
        window.get_wch=read
        self.assertEqual(app.prompt('Имя'),'я')
        self.assertTrue(any('Увеличьте окно' in s for s in window.written))

    def test_resize_during_redraw_does_not_leave_cursor_outside_window(self):
        window=Window(['я','\n']); app=terminal(window)
        window.refresh=lambda:setattr(window,'size',(18,60))
        self.assertEqual(app.prompt('Имя'),'я')

    def test_plan_pagination_does_not_skip_lines_in_a_short_window(self):
        window=Window(size=(18,80)); app=terminal(window)
        window.keys=iter([app.curses.KEY_NPAGE]*10+['\n'])
        app.lines=[('строка '+str(i),'text') for i in range(25)]
        self.assertEqual(app.select('Применить план?',[('yes','Да'),('no','Нет')],'no'),'no')
        self.assertTrue(all('строка '+str(i) in window.written for i in range(25)))

    def test_docker_secrets_are_excluded_from_command_log(self):
        logged=[]
        output=subprocess.CompletedProcess([],0,'SECRET_KEY=PRIVATE','')
        with patch('node.subprocess.run',return_value=output),patch.object(node,'command_log',logged.append):
            node.run('docker','inspect','remnanode')
        self.assertEqual(logged,[])

    def test_environment_secrets_survive_back_without_being_reexported(self):
        app=terminal(Window())
        with patch.object(ui,'screen',app),patch.dict(os.environ,REMNAWAVE_API_TOKEN='private'):
            self.assertEqual(ui.env_secret('REMNAWAVE_API_TOKEN'),'private')
            self.assertNotIn('REMNAWAVE_API_TOKEN',os.environ)
            self.assertEqual(ui.env_secret('REMNAWAVE_API_TOKEN'),'private')


@unittest.skipUnless(sys.platform=='linux','Real curses/PTY is verified on Linux CI')
class PtyTests(unittest.TestCase):
    def test_real_terminal_back_resize_secret_and_apply(self):
        import fcntl
        import pty
        import select
        import signal
        import struct
        import termios
        import time
        with tempfile.TemporaryDirectory() as directory:
            master,slave=pty.openpty()
            fcntl.ioctl(slave,termios.TIOCSWINSZ,struct.pack('HHHH',28,100,0,0))
            before=termios.tcgetattr(slave)
            process=subprocess.Popen([sys.executable,str(Path(__file__).with_name('tui_fixture.py')),directory],
                stdin=slave,stdout=slave,stderr=slave,env=dict(os.environ,TERM='xterm-256color',LANG='C.UTF-8'))
            output=bytearray(); position=0
            def expect(text):
                nonlocal position
                target=text.encode(); deadline=time.monotonic()+15
                while time.monotonic()<deadline:
                    index=output.find(target,position)
                    if index>=0: position=index+len(target); return
                    if select.select([master],[],[],.1)[0]:
                        try: output.extend(os.read(master,65536))
                        except OSError: break
                self.fail('Missing terminal text '+text+'\n'+output.decode(errors='replace')[-3000:])
            def send(value): os.write(master,value.encode())
            try:
                expect('Главное меню'); send('\n')
                expect('Адрес панели'); send('https://old.example.com\n')
                expect('API-токен'); send('\x1b')
                expect('Адрес панели'); send('\x15https://new.example.com\n')
                expect('API-токен'); send('PTY_PRIVATE_TOKEN')
                fcntl.ioctl(slave,termios.TIOCSWINSZ,struct.pack('HHHH',22,80,0,0))
                os.kill(process.pid,signal.SIGWINCH); send('\n')
                expect('Общее имя'); send('🇳🇱 Нидерланды\n')
                expect('Дополнения'); send(' \n')
                expect('Применить план?'); send('\x1bOA\n')
                expect('Готово'); send('\n')
                expect('Главное меню'); send('\x1b')
                self.assertEqual(process.wait(timeout=10),0)
                self.assertEqual(termios.tcgetattr(slave),before)
                self.assertEqual((Path(directory)/'applied').read_text(),'https://new.example.com')
                self.assertNotIn(b'PTY_PRIVATE_TOKEN',output)
                log=(Path(directory)/'logs/installer.log').read_text()
                self.assertNotIn('PTY_PRIVATE_TOKEN',log)
                self.assertIn('Применение',log)
            finally:
                if process.poll() is None: process.kill(); process.wait(timeout=10)
                os.close(master); os.close(slave)

    def test_ctrl_c_restores_terminal_before_any_mutation(self):
        import fcntl
        import pty
        import select
        import signal
        import struct
        import termios
        with tempfile.TemporaryDirectory() as directory:
            master,slave=pty.openpty(); before=termios.tcgetattr(slave)
            fcntl.ioctl(slave,termios.TIOCSWINSZ,struct.pack('HHHH',24,80,0,0))
            process=subprocess.Popen([sys.executable,str(Path(__file__).with_name('tui_fixture.py')),directory],
                stdin=slave,stdout=slave,stderr=slave,env=dict(os.environ,TERM='xterm-256color'))
            try:
                self.assertTrue(select.select([master],[],[],10)[0]); os.read(master,65536)
                os.kill(process.pid,signal.SIGINT)
                self.assertEqual(process.wait(timeout=10),0)
                self.assertEqual(termios.tcgetattr(slave),before)
                self.assertFalse((Path(directory)/'applied').exists())
                self.assertFalse((Path(directory)/'logs').exists())
            finally:
                if process.poll() is None: process.kill(); process.wait(timeout=10)
                os.close(master); os.close(slave)
