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

from screen import Answer,Back,Screen,available,clean,clip,content_start,terminal_profile,width,wrap
import node
import ui


class Window:
    def __init__(self,keys=(),size=(24,80)):
        self.keys=iter(keys); self.size=size; self.written=[]; self.cells={}
    def getmaxyx(self): return self.size
    def erase(self): self.cells={}
    def clear(self): pass
    def refresh(self): pass
    def timeout(self,value): pass
    def move(self,row,col):
        assert 0<=row<self.size[0] and 0<=col<self.size[1]
    def get_wch(self): return next(self.keys)
    def addstr(self,row,col,text,*style):
        assert 0<=row<self.size[0] and col+width(text)<self.size[1]
        self.written.append(text)
        self.cells[row]=text


def terminal(window):
    constants={name:100+i for i,name in enumerate(('KEY_LEFT','KEY_RIGHT','KEY_UP','KEY_DOWN',
        'KEY_PPAGE','KEY_NPAGE','KEY_HOME','KEY_END','KEY_DC','KEY_BACKSPACE','KEY_ENTER','KEY_RESIZE'))}
    return Screen(window,SimpleNamespace(**constants,A_REVERSE=0,error=RuntimeError,has_colors=lambda:False,curs_set=lambda n:None),'test')


class ScreenTests(unittest.TestCase):
    def test_colour_compatibility_and_explicit_basic_mode(self):
        with patch('sys.stdin.isatty',return_value=True),patch('sys.stdout.isatty',return_value=True):
            for term,mode,expected in [('xterm','auto','xterm-256color'),
                    ('xterm-color','auto','xterm-256color'),('linux','auto','linux'),
                    ('vt100','auto','vt100'),('xterm','basic','xterm'),
                    ('xterm-256color','basic','xterm-256color')]:
                with self.subTest(term=term,mode=mode),patch.dict(os.environ,TERM=term,REMNAYGG_COLORS=mode):
                    os.environ.pop('NO_COLOR',None)
                    self.assertEqual(terminal_profile(),expected)
                    self.assertEqual(os.environ['TERM'],term)
            with patch.dict(os.environ,TERM='xterm',NO_COLOR='1'):
                self.assertEqual(terminal_profile(),'xterm')

    def test_tiny_resize_does_not_block_selection(self):
        window=Window(size=(18,4)); app=terminal(window)
        def resize():
            window.size=(24,80)
            return '\n'
        window.get_wch=resize
        self.assertEqual(app.select('中文 · Нода',[('a','Первая нода')],'a'),'a')

    def test_long_names_keep_identifying_suffix_and_full_detail(self):
        window=Window(['\n'],size=(18,54)); app=terminal(window)
        name='Одинаковое длинное начало '*4+'ВТОРАЯ НОДА'
        app.select('Нода',[('b',name)],'b')
        self.assertTrue(any('ВТОРАЯ НОДА' in line and '…' in line for line in window.written))
        self.assertEqual(app.lines,[])

    def test_logs_preserve_indentation_color_errors_and_reach_end(self):
        window=Window(size=(18,54)); app=terminal(window)
        window.keys=iter([app.curses.KEY_END,'\n'])
        tones=[]
        original=app.line
        def line(row,text,tone='text',reverse=False):
            tones.append((text,tone)); original(row,text,tone,reverse)
        app.line=line
        app.view('Журнал','{\n    "data": true\n}\nWARNING retry\nERROR failed\n'+'x'*500+'\nLAST_LINE')
        self.assertIn(('    "data": true','text'),tones)
        self.assertIn(('WARNING retry','warning'),tones)
        self.assertIn(('ERROR failed','error'),tones)
        self.assertIn('LAST_LINE',window.written)

    def test_log_redacts_known_secrets_and_terminal_escape_sequences(self):
        window=Window(['\n']); app=terminal(window); app.secrets.add('SESSION_SECRET')
        app.view('Журнал','\x1b[31mERROR\x1b[0m SESSION_SECRET\nSECRET_KEY=OTHER\n'+
                 '\x1b]8;;https://untrusted.example\x1b\\label\x1b]8;;\x1b\\')
        rendered='\n'.join(window.written)
        self.assertNotIn('SESSION_SECRET',rendered)
        self.assertNotIn('OTHER',rendered)
        self.assertNotIn('untrusted.example',rendered)
        self.assertIn('label',rendered)

    def test_optional_missing_service_is_distinct_from_failure(self):
        app=terminal(Window())
        result=subprocess.CompletedProcess([],0,
            'Id=remna-ygg-ssh.service\nLoadState=not-found\nActiveState=inactive\n\n'+
            'Id=yggdrasil.service\nLoadState=loaded\nActiveState=failed\nSubState=failed\n')
        with patch('screen.subprocess.run',return_value=result): report=app.service_report()
        self.assertIn('Доступ к SSH: не настроено',report)
        self.assertIn('Yggdrasil: FAILED',report)

    def test_idle_certificate_job_with_active_timer_is_not_a_failure(self):
        app=terminal(Window())
        result=subprocess.CompletedProcess([],0,
            'Id=remna-hy2-certs.service\nLoadState=loaded\nActiveState=inactive\nResult=success\n\n'+
            'Id=remna-hy2-certs.timer\nLoadState=loaded\nActiveState=active\nSubState=waiting\nResult=success\n')
        with patch('screen.subprocess.run',return_value=result): report=app.service_report()
        self.assertIn('Обновление сертификата: ожидает запуска по таймеру',report)
        self.assertIn('Таймер сертификата: запуск по расписанию включён',report)
        self.assertNotIn('FAILED',report)

    def test_failed_certificate_job_is_not_hidden_by_active_timer(self):
        app=terminal(Window())
        result=subprocess.CompletedProcess([],0,
            'Id=remna-hy2-certs.service\nLoadState=loaded\nActiveState=inactive\nResult=exit-code\n\n'+
            'Id=remna-hy2-certs.timer\nLoadState=loaded\nActiveState=active\n')
        with patch('screen.subprocess.run',return_value=result): report=app.service_report()
        self.assertIn('Обновление сертификата: FAILED',report)
        self.assertNotIn('ожидает запуска',report)

    def test_multiselect_help_fits_small_terminal_without_losing_back_key(self):
        window=Window(['\n'],size=(18,54)); app=terminal(window)
        app.select('Протоколы',[(str(n),'Протокол '+str(n)) for n in range(20)],[],True)
        footer=' '.join(window.cells.values())
        self.assertIn('Пробел',footer); self.assertIn('Esc',footer)

    def test_menu_and_input_stay_next_to_explanation_in_tall_windows(self):
        for height in (24,40,60):
            window=Window(['\n'],size=(height,100)); app=terminal(window)
            app.lines=[('Дополнения для вашей ноды.','muted')]
            app.select('Выберите действие',[('setup','Протоколы и доступ'),('exit','Выход')],'setup')
            start=content_start(height,100)
            self.assertEqual(window.cells[start],'Дополнения для вашей ноды.')
            self.assertEqual(window.cells[start+2],'Выберите действие')
            self.assertIn('Протоколы и доступ',window.cells[start+4])
            self.assertIn('Esc',window.cells[start+7])
            window.keys=iter(['\n']); app.position=0; app.answers=[]
            app.prompt('Адрес панели','https://panel.example.com')
            self.assertEqual(app.input_row,start+4)
            self.assertIn('https://panel.example.com',window.cells[start+4])

    def test_all_control_types_fit_compact_window_with_wrapped_questions(self):
        for label in ('Что добавить?','Каким сквадам выдать доступ?',
                      'Разрешить также SSH через Yggdrasil?',
                      'Сквады для VLESS Reality через Yggdrasil; пусто — настрою в панели'):
            window=Window(size=(18,54)); app=terminal(window)
            app.lines=[('Пояснение '+str(i),'text') for i in range(30)]
            app.draw(label,'Enter далее · Esc назад', [('Вариант '+str(i),'text',False) for i in range(5)])
            self.assertIn('Esc',window.cells[17])
            self.assertEqual(len([text for text in window.cells.values() if text.startswith('Вариант')]),5)
            self.assertTrue(any('PgUp/PgDn' in text for text in window.cells.values()))

    def test_eight_color_terminals_keep_help_text_visible(self):
        colors=[]
        curses=SimpleNamespace(has_colors=lambda:True,start_color=lambda:None,use_default_colors=lambda:None,
            COLORS=8,init_pair=lambda index,color,background:colors.append(color),color_pair=lambda index:index)
        with patch.dict(os.environ,{},clear=False):
            os.environ.pop('NO_COLOR',None)
            Screen(Window(),curses,'test')
        self.assertTrue(all(1<=color<=7 for color in colors))

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
    def test_real_terminal_uses_gradient_without_answering_queries(self):
        import pty
        import termios
        import json
        for term,mode,expected in [('xterm','auto',6),('xterm','basic',0),('linux','auto',0)]:
            with self.subTest(term=term,mode=mode),tempfile.TemporaryDirectory() as directory:
                master,slave=pty.openpty(); before=termios.tcgetattr(slave)
                path=Path(directory)/'profile.json'
                code=('import json,os,curses,sys; from pathlib import Path; '
                      'from screen import terminal_profile,Screen; '
                      'os.environ["TERM"]=terminal_profile(); '
                      'app=curses.wrapper(lambda window:Screen(window,curses,"test")); '
                      'Path(sys.argv[1]).write_text(json.dumps(sum(key.startswith("banner") for key in app.palette)))')
                process=subprocess.Popen([sys.executable,'-c',code,str(path)],stdin=slave,stdout=slave,stderr=slave,
                    env={k:v for k,v in dict(os.environ,TERM=term,REMNAYGG_COLORS=mode).items()
                         if k not in ('NO_COLOR','COLORTERM','TERM_PROGRAM')})
                try:
                    self.assertEqual(process.wait(timeout=5),0)
                    self.assertEqual(json.loads(path.read_text()),expected)
                    self.assertEqual(termios.tcgetattr(slave),before)
                finally:
                    if process.poll() is None: process.kill(); process.wait(timeout=5)
                    os.close(master); os.close(slave)

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
                expect('API-токен'); send('  PTY_PRIVATE_TOKEN  ')
                fcntl.ioctl(slave,termios.TIOCSWINSZ,struct.pack('HHHH',22,80,0,0))
                os.kill(process.pid,signal.SIGWINCH); send('\n')
                expect('Общее имя'); send('  🇳🇱 Нидерланды  \n')
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
