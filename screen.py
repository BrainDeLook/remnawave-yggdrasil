"""SSH-friendly terminal screens. Navigation replays only the read-only wizard."""
from __future__ import annotations
from dataclasses import dataclass
import locale
import os
from pathlib import Path
import re
import subprocess
import sys
import unicodedata

LOG_DIR=Path('/var/log/remnawave-yggdrasil')


class Back(Exception):
    def __init__(self,index: int): self.index=index


@dataclass
class Answer:
    key: tuple
    value: str | list[str]


def width(text: str) -> int:
    return sum(0 if unicodedata.combining(c) else 2 if unicodedata.east_asian_width(c) in ('W','F') else 1 for c in text)


def clip(text: str, columns: int) -> str:
    result=[]; used=0
    for char in text:
        size=width(char)
        if used+size>columns: break
        result.append(char); used+=size
    return ''.join(result)


def wrap(text: str,columns: int) -> list[str]:
    lines=[]
    while text:
        part=clip(text,columns)
        if len(part)<len(text) and part.rfind(' ')>len(part)//2: part=part[:part.rfind(' ')]
        lines.append(part); text=text[len(part):].lstrip(' ')
    return lines or ['']


def available(argv: list[str]) -> bool:
    plain={'--plain','--yes','--check','--result','--network-only','--sync-certs','--help','-h','--version'}
    return (sys.stdin.isatty() and sys.stdout.isatty() and os.environ.get('TERM','') not in ('','dumb')
            and not plain.intersection(argv))


class Screen:
    def __init__(self,window,curses,version: str):
        self.window=window; self.curses=curses; self.version=version
        self.title='Главное меню'; self.lines=[]; self.offset=0; self.page_size=1
        self.answers=[]; self.position=0; self.replay_until=0
        self.environment={}; self.applying=False; self.log=None
        self.palette={}
        if curses.has_colors() and 'NO_COLOR' not in os.environ:
            curses.start_color(); curses.use_default_colors()
            for index,(tone,color) in enumerate([('text',7),('muted',8),('section',14),('question',11),
                 ('value',10),('success',10),('example',12),('warning',11),('error',9)],1):
                if curses.COLORS<16: color=7 if tone=='muted' else color%8
                curses.init_pair(index,color,-1); self.palette[tone]=curses.color_pair(index)

    def line(self,row: int,text: str,tone: str='text',reverse: bool=False):
        height,columns=self.window.getmaxyx()
        if not 0<=row<height: return
        text=re.sub(r'\x1b\[[0-?]*[ -/]*[@-~]','',text)
        value=clip(''.join(c if c.isprintable() else ' ' for c in text),max(0,columns-4))
        style=self.palette.get(tone,0) | (self.curses.A_REVERSE if reverse else 0)
        self.window.addstr(row,2,value,style)

    def draw(self,label: str='',footer: str='PgUp/PgDn — текст   Esc — назад',extra: list=()):
        window=self.window; window.erase(); height,columns=window.getmaxyx()
        if height<18 or columns<54:
            self.line(1,'Увеличьте окно: нужно от 54 × 18.','warning')
            self.line(3,'Размер сейчас: '+str(columns)+' × '+str(height))
            window.refresh(); return False
        self.line(0,'Remnawave + Yggdrasil · '+self.version,'section')
        self.line(2,self.title,'section')
        rows=[]
        for text,tone in self.lines:
            for paragraph in text.splitlines() or ['']:
                rows.extend((part,tone) for part in wrap(paragraph,columns-5))
        label_row=min(height-5,min((row for row,*_ in extra),default=height-3)-2)
        space=max(0,label_row-5)
        self.page_size=max(1,space)
        self.offset=max(0,min(self.offset,max(0,len(rows)-space)))
        for row,(text,tone) in enumerate(rows[self.offset:self.offset+space],4): self.line(row,text,tone)
        for row,text,tone,reverse in extra: self.line(row,text,tone,reverse)
        self.line(label_row,label,'question')
        self.line(height-1,footer,'muted')
        window.refresh(); return True

    def page(self,title: str):
        self.title=title; self.lines=[]; self.offset=0
        self.window.clear()
        if self.position>=self.replay_until: self.draw(footer='Подготовка…')

    def say(self,text: str,tone: str='text'):
        self.lines.append((text.strip('\n'),tone))
        if self.log:
            self.log.write(text+'\n'); self.log.flush()
        if self.position>=self.replay_until:
            self.offset=max(0,len(self.lines)*3)
            self.draw(footer='Выполняется текущий этап. Журнал — в меню диагностики.' if self.applying else 'Подготовка…')

    def remember(self,key: tuple,default,read):
        index=self.position; self.position+=1
        previous=self.answers[index] if index<len(self.answers) else None
        if previous is not None and previous.key!=key:
            del self.answers[index:]; previous=None
        if previous is not None and index<self.replay_until: return previous.value
        answer=read(previous.value if previous is not None else default)
        if previous is None or answer!=previous.value:
            del self.answers[index:]; self.answers.append(Answer(key,answer))
        return answer

    def key(self):
        value=self.window.get_wch()
        if value=='\x03': raise KeyboardInterrupt
        if value in ('\x1b',self.curses.KEY_LEFT):
            if self.applying: return None
            raise Back(self.position-2)
        if value==self.curses.KEY_PPAGE: self.offset=max(0,self.offset-self.page_size)
        if value==self.curses.KEY_NPAGE: self.offset+=self.page_size
        return value

    def prompt(self,label: str,default: str='',secret: bool=False) -> str:
        def read(previous):
            value=previous if not secret else ''; cursor=len(value)
            self.curses.curs_set(1)
            try:
                while True:
                    height,columns=self.window.getmaxyx()
                    footer='Enter — продолжить   Esc — назад   Ctrl+U — очистить'
                    display='[ввод скрыт'+('; Enter — оставить введённое' if previous else '')+']' if secret else value
                    start=0
                    if not secret:
                        while width(value[start:cursor])>max(1,columns-7): start+=1
                        display=value[start:]
                    ready=self.draw(label,footer,[(height-3,'> '+display,'value',False)])
                    if ready:
                        # refresh() can process SIGWINCH after the layout was calculated.
                        current_height,current_columns=self.window.getmaxyx()
                        column=4 if secret else 4+width(value[start:cursor])
                        self.window.move(max(0,current_height-3),min(column,max(0,current_columns-2)))
                        self.window.refresh()
                    key=self.window.get_wch()
                    if key=='\x03': raise KeyboardInterrupt
                    if key=='\x1b': raise Back(self.position-2)
                    if key in ('\n','\r',self.curses.KEY_ENTER):
                        if ready: return value.strip() or previous
                        continue
                    if key in ('\b','\x7f',self.curses.KEY_BACKSPACE) and cursor:
                        value=value[:cursor-1]+value[cursor:]; cursor-=1
                    elif key==self.curses.KEY_DC: value=value[:cursor]+value[cursor+1:]
                    elif key==self.curses.KEY_LEFT: cursor=max(0,cursor-1)
                    elif key==self.curses.KEY_RIGHT: cursor=min(len(value),cursor+1)
                    elif key==self.curses.KEY_HOME: cursor=0
                    elif key==self.curses.KEY_END: cursor=len(value)
                    elif key=='\x15': value=''; cursor=0
                    elif key==self.curses.KEY_PPAGE: self.offset=max(0,self.offset-self.page_size)
                    elif key==self.curses.KEY_NPAGE: self.offset+=self.page_size
                    elif isinstance(key,str) and key.isprintable():
                        value=value[:cursor]+key+value[cursor:]; cursor+=len(key)
            finally: self.curses.curs_set(0)
        return self.remember(('secret' if secret else 'input',label),default,read)

    def select(self,label: str,choices: list[tuple[str,str]],default,multiple: bool=False,allow_empty: bool=True):
        def read(previous):
            if not choices:
                if allow_empty: return []
                from api import Error
                raise Error('Нет доступных вариантов: '+label)
            selected=set(previous) if multiple else set()
            index=next((i for i,(key,_) in enumerate(choices) if key==(previous if not multiple else None)),0)
            error=''
            while True:
                height,_=self.window.getmaxyx(); count=max(1,min(len(choices),height//2-2))
                start=max(0,min(index-count//2,len(choices)-count))
                rows=[]
                for row,(key,name) in enumerate(choices[start:start+count],height-6-count):
                    number=start+len(rows)
                    prefix=('[x] ' if key in selected else '[ ] ') if multiple else '  '
                    rows.append((row,('→ ' if number==index else '  ')+prefix+name,'value' if number==index else 'text',number==index))
                footer='↑↓ выбор · '+('Пробел отметка · ' if multiple else '')+'Enter далее · Esc назад · PgUp/PgDn текст'
                ready=self.draw(error or label,footer,rows)
                key=self.key()
                if not ready: continue
                if key==self.curses.KEY_UP: index=(index-1)%len(choices)
                elif key==self.curses.KEY_DOWN: index=(index+1)%len(choices)
                elif key==self.curses.KEY_HOME: index=0
                elif key==self.curses.KEY_END: index=len(choices)-1
                elif key==' ' and multiple:
                    item=choices[index][0]
                    if item in selected: selected.remove(item)
                    else: selected.add(item)
                elif key in ('\n','\r',self.curses.KEY_ENTER):
                    if not multiple: return choices[index][0]
                    if selected or allow_empty: return [key for key,_ in choices if key in selected]
                    error='Отметьте хотя бы одно дополнение пробелом.'
        return self.remember(('multi' if multiple else 'select',label,tuple(sorted(k for k,_ in choices))),default,read)

    def view(self,title: str,text: str=''):
        if text: self.page(title); self.lines=[(line,'text') for line in text.splitlines()]
        while True:
            self.draw('','↑↓ / PgUp / PgDn — прокрутка   Enter / Esc — в меню')
            key=self.window.get_wch()
            if key in ('\x1b','\n','\r',self.curses.KEY_ENTER): return
            if key=='\x03': raise KeyboardInterrupt
            if key in (self.curses.KEY_DOWN,self.curses.KEY_NPAGE): self.offset+=1 if key==self.curses.KEY_DOWN else self.page_size
            if key in (self.curses.KEY_UP,self.curses.KEY_PPAGE): self.offset=max(0,self.offset-(1 if key==self.curses.KEY_UP else self.page_size))
            if key==self.curses.KEY_HOME: self.offset=0

    def begin_apply(self):
        self.applying=True; self.answers.clear(); self.replay_until=0
        LOG_DIR.mkdir(parents=True,exist_ok=True,mode=0o700); LOG_DIR.chmod(0o700)
        path=LOG_DIR/'installer.log'
        # O_NOFOLLOW prevents a planted symlink from redirecting the root-owned log.
        descriptor=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_TRUNC|os.O_NOFOLLOW,0o600)
        os.fchmod(descriptor,0o600); self.log=os.fdopen(descriptor,'w',encoding='utf-8')
        for text,_ in self.lines: self.log.write(text+'\n')
        import node
        node.command_log=self.command_log

    def command_log(self,text: str):
        if self.log: self.log.write(text+'\n'); self.log.flush()

    def finish(self):
        import node
        node.command_log=None
        if self.log: self.log.close(); self.log=None
        self.applying=False; self.answers.clear(); self.position=0; self.replay_until=0

    def wizard(self,callback,argv):
        self.answers=[]; self.replay_until=0
        while True:
            self.position=0
            try:
                callback(argv); self.view('Результат'); return
            except Back as back:
                if self.applying: raise RuntimeError('Возврат после применения запрещён')
                if back.index<0: return
                self.replay_until=back.index
                self.page('Возврат к предыдущему вопросу…')

    def diagnostics(self):
        choices=[('status','Состояние служб'),('ygg','Журнал Yggdrasil'),('node','Журнал Remnanode'),
                 ('installer','Журнал последнего применения'),('back','Назад')]
        while True:
            self.page('Диагностика · только просмотр'); self.position=0; self.answers=[]
            selected=self.select('Что показать?',choices,'status')
            if selected=='back': return
            if selected=='installer':
                path=LOG_DIR/'installer.log'
                text=path.read_text() if path.exists() else 'Журнала пока нет. Он создаётся после подтверждения применения.'
                text='\n'.join(text.splitlines()[-300:])
            else:
                command={'status':['systemctl','--no-pager','--plain','status','yggdrasil','remna-ygg-access','remna-ygg-ssh','remna-hy2-certs.timer'],
                         'ygg':['journalctl','-u','yggdrasil','-n','100','--no-pager'],
                         'node':['docker','logs','--tail','100','remnanode']}[selected]
                self.say('Загружаю журнал…','muted')
                output=subprocess.run(command,capture_output=True,text=True,timeout=30)
                text=output.stdout+output.stderr
            self.view('Диагностика',text or 'Нет записей.')


def launch(callback,result,version: str,argv: list[str]) -> bool:
    try:
        import curses
    except ImportError: return False
    # Locale is for wide-character input/rendering only; commands keep LC_ALL=C.
    locale.setlocale(locale.LC_CTYPE,'C.UTF-8')
    window=None
    try:
        window=curses.initscr(); curses.noecho(); curses.cbreak(); window.keypad(True)
        curses.set_escdelay(40); curses.curs_set(0)
        app=Screen(window,curses,version)
    except (curses.error,KeyboardInterrupt) as exc:
        if window is not None:
            window.keypad(False); curses.nocbreak(); curses.echo(); curses.endwin()
        return isinstance(exc,KeyboardInterrupt)
    import ui
    from api import Error
    ui.screen=app
    interrupted=False
    try:
        first=True
        while True:
            app.finish(); app.page('Главное меню')
            app.say('Настройка существующей ноды Remnawave. До подтверждения\nможно вернуться назад и изменить ответы.')
            in_menu=True
            try:
                if first and argv: selected='setup'
                else:
                    selected=app.select('Выберите действие',[
                        ('setup','Установить / изменить дополнительные протоколы'),
                        ('network','Настроить Yggdrasil и SSH'),
                        ('result','Показать подключения'),('logs','Диагностика и журналы'),('exit','Выход')],'setup')
                first=False; app.answers=[]; app.position=0
                in_menu=False
                if selected=='exit': return True
                if selected in ('setup','network'):
                    app.wizard(callback,['--protocol','ygg'] if selected=='network' else argv)
                elif selected=='result': result(); app.view('Сохранённые подключения')
                else: app.diagnostics()
            except Back:
                if in_menu: return True
                continue
            except (Error,OSError,ValueError,KeyError,subprocess.SubprocessError) as exc:
                app.page('Не удалось завершить действие')
                app.say(str(exc),'error')
                app.say('Часть шагов могла завершиться. Исправьте причину и повторите запуск.' if app.applying else
                        'Применение не подтверждено. Рабочие настройки не менялись.','warning')
                if app.applying: app.say('Журнал: '+str(LOG_DIR/'installer.log'),'example')
                app.view('Ошибка')
    except KeyboardInterrupt:
        interrupted=app.applying
        if app.applying: app.command_log('Прервано пользователем: часть шагов могла завершиться.')
        return True
    finally:
        app.finish(); app.environment.clear(); ui.screen=None
        window.keypad(False); curses.nocbreak(); curses.echo(); curses.endwin()
        if interrupted: print('Применение прервано. Часть шагов могла завершиться; журнал: '+str(LOG_DIR/'installer.log'))
