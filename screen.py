"""SSH-friendly terminal screens. Navigation replays only the read-only wizard."""
from __future__ import annotations
from collections import deque
from dataclasses import dataclass
import locale
import os
from pathlib import Path
import re
import subprocess
import sys
from textwrap import dedent
import unicodedata

LOG_DIR=Path('/var/log/remnawave-yggdrasil')
ESCAPES=re.compile(r'\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\))')
BANNER=(
    '██████╗ ███████╗███╗   ███╗███╗   ██╗ █████╗ ██╗   ██╗ ██████╗  ██████╗ ',
    '██╔══██╗██╔════╝████╗ ████║████╗  ██║██╔══██╗╚██╗ ██╔╝██╔════╝ ██╔════╝ ',
    '██████╔╝█████╗  ██╔████╔██║██╔██╗ ██║███████║ ╚████╔╝ ██║  ███╗██║  ███╗',
    '██╔══██╗██╔══╝  ██║╚██╔╝██║██║╚██╗██║██╔══██║  ╚██╔╝  ██║   ██║██║   ██║',
    '██║  ██║███████╗██║ ╚═╝ ██║██║ ╚████║██║  ██║   ██║   ╚██████╔╝╚██████╔╝',
    '╚═╝  ╚═╝╚══════╝╚═╝     ╚═╝╚═╝  ╚═══╝╚═╝  ╚═╝   ╚═╝    ╚═════╝  ╚═════╝ ',
)


def clean(text: str) -> str:
    """Normalize external terminal output before measuring or rendering it."""
    text=ESCAPES.sub('',text).expandtabs(4)
    return ''.join(c if c.isprintable() or c=='\n' else ' ' for c in text)


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
    columns=max(2,columns)
    lines=[]
    while text:
        part=clip(text,columns)
        if len(part)<len(text) and part.rfind(' ')>len(part)//2: part=part[:part.rfind(' ')]
        lines.append(part); text=text[len(part):].lstrip(' ')
    return lines or ['']


def elide(text: str,columns: int) -> str:
    """Keep both the beginning and identifying suffix of a long menu title."""
    if width(text)<=columns: return text
    tail=''; limit=max(0,(columns-1)//2)
    for char in reversed(text):
        if width(char+tail)>limit: break
        tail=char+tail
    return clip(text,max(0,columns-1-width(tail)))+'…'+tail


def available(argv: list[str]) -> bool:
    plain={'--plain','--yes','--check','--result','--network-only','--sync-certs','--help','-h','--version'}
    return (sys.stdin.isatty() and sys.stdout.isatty() and os.environ.get('TERM','') not in ('','dumb')
            and not plain.intersection(argv))


def content_start(height: int,columns: int) -> int:
    return 9 if height>=25 and columns>=90 else 4


class Screen:
    def __init__(self,window,curses,version: str):
        self.window=window; self.curses=curses; self.version=version
        self.title='Главное меню'; self.lines=[]; self.offset=0; self.page_size=1; self.input_row=6
        self.answers=[]; self.position=0; self.replay_until=0
        self.environment={}; self.secrets=set(); self.applying=False; self.log=None
        self.palette={}
        if curses.has_colors() and 'NO_COLOR' not in os.environ:
            curses.start_color(); curses.use_default_colors()
            for index,(tone,color) in enumerate([('text',7),('muted',8),('section',14),('question',11),
                 ('value',10),('success',10),('example',12),('warning',11),('error',9)],1):
                if curses.COLORS<16: color=7 if tone=='muted' else color%8
                curses.init_pair(index,color,-1); self.palette[tone]=curses.color_pair(index)
            # Fixed xterm-256 colours: blue -> cyan -> ice blue, one step per row.
            # Do not alter the terminal's global palette or depend on theme-defined blue.
            if curses.COLORS>=256:
                for row,color in enumerate((39,45,81,117,153,195),1):
                    curses.init_pair(9+row,color,-1)
                    self.palette['banner'+str(row)]=curses.color_pair(9+row)

    def line(self,row: int,text: str,tone: str='text',reverse: bool=False):
        height,columns=self.window.getmaxyx()
        if not 0<=row<height: return
        value=clip(clean(text).replace('\n',' '),max(0,columns-4))
        style=self.palette.get(tone,0) | (self.curses.A_REVERSE if reverse else 0)
        self.window.addstr(row,2,value,style)

    def draw(self,label: str='',footer: str='Esc — назад',extra: list=()):
        window=self.window; window.erase(); height,columns=window.getmaxyx()
        if height<18 or columns<54:
            self.line(1,'Увеличьте окно: нужно от 54 × 18.','warning')
            self.line(3,'Размер сейчас: '+str(columns)+' × '+str(height))
            window.refresh(); return False
        inner=columns-6; body_row=content_start(height,columns)
        brand=' RemnaYgg '
        version=' v'+self.version+' '
        self.line(0,'╭─'+brand+'─'*max(0,inner-width(brand+version)-1)+version+'╮','section')
        title=elide(self.title,inner-4)
        if body_row==9:
            for row,art in enumerate(BANNER,1):
                left=(inner-width(art))//2
                tone='banner'+str(row) if 'banner'+str(row) in self.palette else 'section'
                self.line(row,'│'+' '*left+art+' '*(inner-left-width(art))+'│',tone)
            self.line(7,'╰─ '+title+' '+'─'*(inner-3-width(title))+'╯','section')
        else:
            self.line(1,'│ '+title+' '*(inner-1-width(title))+'│','section')
            self.line(2,'╰'+'─'*inner+'╯','section')
        rows=[]
        for text,tone in self.lines:
            for paragraph in text.splitlines() or ['']:
                rows.extend((part,tone) for part in wrap(clean(paragraph),columns-5))
        # Body, question and controls form one block; only long bodies scroll.
        labels=wrap(clean(label),columns-5) if label else []
        footers=wrap(footer,columns-5)
        space=max(1,height-body_row-3-len(extra)-len(labels)-len(footers)) if label or extra else height-body_row-1-len(footers)
        self.page_size=max(1,space)
        self.offset=max(0,min(self.offset,max(0,len(rows)-space)))
        visible=rows[self.offset:self.offset+space]
        for row,(text,tone) in enumerate(visible,body_row): self.line(row,text,tone)
        label_row=body_row+len(visible)+(1 if visible else 0)
        for row,part in enumerate(labels,label_row): self.line(row,part,'question')
        self.input_row=label_row+len(labels)+1
        for index,(text,tone,reverse) in enumerate(extra): self.line(self.input_row+index,text,tone,reverse)
        if len(rows)>space:
            self.line(body_row-1,f'Текст: PgUp/PgDn · {self.offset+1}–{self.offset+len(visible)} из {len(rows)}','muted')
        footer_row=min(height-len(footers),self.input_row+len(extra)+1)
        for row,part in enumerate(footers,footer_row): self.line(row,part,'muted')
        window.refresh(); return True

    def page(self,title: str):
        self.title=title; self.lines=[]; self.offset=0
        if self.log: self.command_log('\n[ЭТАП] '+title)
        self.window.clear()
        if self.position>=self.replay_until: self.draw(footer='Подготовка…')

    def say(self,text: str,tone: str='text'):
        text=self.redact(dedent(clean(text)).strip('\n'))
        self.lines.append((text,tone))
        if self.log:
            self.log.write(text+'\n'); self.log.flush()
        if self.position>=self.replay_until:
            self.offset=sys.maxsize if self.applying else 0
            self.draw(footer='Выполняется текущий этап. Журнал — в меню диагностики.' if self.applying else 'Подготовка…')

    def remember(self,key: tuple,default,read):
        index=self.position; self.position+=1
        previous=self.answers[index] if index<len(self.answers) else None
        if previous is not None and previous.key!=key:
            del self.answers[index:]; previous=None
        if previous is not None and index<self.replay_until: return previous.value
        answer=read(previous.value if previous is not None else default)
        if key[0]=='secret' and answer: self.secrets.add(answer)
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
                    _,columns=self.window.getmaxyx()
                    footer='Enter — продолжить   Esc — назад   Ctrl+U — очистить'
                    display='[ввод скрыт'+('; Enter — оставить введённое' if previous else '')+']' if secret else value
                    start=0
                    if not secret:
                        while width(value[start:cursor])>max(1,columns-7): start+=1
                        display=value[start:]
                    ready=self.draw(label,footer,[('> '+display,'value',False)])
                    if ready:
                        # refresh() can process SIGWINCH after the layout was calculated.
                        current_height,current_columns=self.window.getmaxyx()
                        column=4 if secret else 4+width(value[start:cursor])
                        self.window.move(min(self.input_row,max(0,current_height-2)),min(column,max(0,current_columns-2)))
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
                height,columns=self.window.getmaxyx()
                count=max(1,min(len(choices),height-content_start(height,columns)-8-len(wrap(label,columns-5))))
                start=max(0,min(index-count//2,len(choices)-count))
                rows=[]
                for key,name in choices[start:start+count]:
                    number=start+len(rows)
                    prefix=('[x] ' if key in selected else '[ ] ') if multiple else '  '
                    marker=('› ' if number==index else '  ')+prefix
                    text=marker+elide(clean(name),max(1,columns-4-width(marker)))
                    rows.append((text,'section' if number==index else 'text',number==index))
                detail=wrap('Выбрано: '+clean(choices[index][1]),columns-5)
                if any(width(name)>columns-10 for _,name in choices):
                    # A scrollable copy exposes the complete selected name, including its suffix.
                    self.lines=base_lines+[(line,'example') for line in detail]
                footer='↑↓ выбор · '+('Пробел отметка · ' if multiple else '')+'Enter далее · Esc назад'
                choice_label=f'{label} · {index+1}/{len(choices)}' if len(choices)>count else label
                ready=self.draw(error or choice_label,footer,rows)
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
        base_lines=list(self.lines)
        try: return self.remember(('multi' if multiple else 'select',label,tuple(sorted(k for k,_ in choices))),default,read)
        finally: self.lines=base_lines

    def view(self,title: str,text: str=''):
        if text:
            self.page(title)
            self.lines=[]
            for line in self.redact(clean(text)).splitlines():
                tone='error' if re.search(r'(^E:|\b(error|failed|failure|fatal|panic|ошибка)\b)',line,re.I) else (
                     'warning' if re.search(r'\b(warn(?:ing)?|timeout|предупреждение)\b',line,re.I) else (
                     'section' if line.startswith('[ЭТАП]') else (
                     'success' if re.search(r': (работает|выполнено|запуск по расписанию включён)$',line) else 'text')))
                self.lines.append((line,tone))
        self.offset=0
        while True:
            footer='↑↓ / PgUp/PgDn текст · Home/End начало/конец\nEnter / Esc — назад' if len(self.lines)>self.page_size else 'Enter / Esc — назад'
            self.draw('',footer)
            key=self.window.get_wch()
            if key in ('\x1b','\n','\r',self.curses.KEY_ENTER): return
            if key=='\x03': raise KeyboardInterrupt
            if key in (self.curses.KEY_DOWN,self.curses.KEY_NPAGE): self.offset+=1 if key==self.curses.KEY_DOWN else self.page_size
            if key in (self.curses.KEY_UP,self.curses.KEY_PPAGE): self.offset=max(0,self.offset-(1 if key==self.curses.KEY_UP else self.page_size))
            if key==self.curses.KEY_HOME: self.offset=0
            if key==self.curses.KEY_END: self.offset=sys.maxsize

    def redact(self,text: str) -> str:
        for value in self.secrets | set(self.environment.values()):
            if value: text=text.replace(value,'[скрыто]')
        return re.sub(r'(?i)(authorization:\s*(?:bearer\s+)?|SECRET_KEY=|api[_-]?token=)\S+',r'\1[скрыто]',text)

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
        if self.log: self.log.write(self.redact(clean(text))+'\n'); self.log.flush()

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
                 ('cert','Журнал сертификата Hysteria2'),('installer','Журнал последнего применения'),
                 ('bootstrap','Журнал загрузчика'),('back','Назад')]
        while True:
            self.page('Диагностика · только просмотр'); self.position=0; self.answers=[]
            selected=self.select('Что показать?',choices,'status')
            if selected=='back': return
            if selected in ('installer','bootstrap'):
                path=LOG_DIR/('installer.log' if selected=='installer' else 'bootstrap.log')
                if path.exists():
                    with path.open(errors='replace') as log: text=''.join(deque(log,300))
                else:
                    text=('Журнала пока нет. Он создаётся после подтверждения применения.' if selected=='installer' else
                          'Журнала нет: загрузчику не потребовалась установка зависимостей.')
            elif selected=='status':
                text=self.service_report()
            else:
                command={'ygg':['journalctl','-u','yggdrasil','-u','remna-ygg-network','-u','remna-ygg-access','-u','remna-ygg-ssh','-n','100','--no-pager'],
                         'cert':['journalctl','-u','remna-hy2-certs','-n','100','--no-pager'],
                         'node':['docker','logs','--tail','100','remnanode']}[selected]
                self.say('Загружаю журнал…','muted')
                output=subprocess.run(command,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True,errors='replace',timeout=30)
                text=output.stdout
                if output.returncode: text=f'Команда завершилась с кодом {output.returncode}.\n'+text
            self.view(dict(choices)[selected],text or 'Нет записей.')

    def service_report(self) -> str:
        """Read systemd's property boundary, including optional absent units."""
        units={'yggdrasil.service':'Yggdrasil','remna-ygg-network.service':'Маршруты Yggdrasil',
               'remna-ygg-access.service':'Доступ к VPN','remna-ygg-ssh.service':'Доступ к SSH',
               'remna-hy2-certs.service':'Обновление сертификата',
               'remna-hy2-certs.timer':'Таймер сертификата'}
        result=subprocess.run(['systemctl','show','--no-pager','--property=Id,LoadState,ActiveState,SubState,Result',*units],
                              stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True,errors='replace',timeout=30)
        states={}
        for block in result.stdout.strip().split('\n\n'):
            values=dict(line.split('=',1) for line in block.splitlines() if '=' in line)
            if 'Id' in values: states[values['Id']]=values
        lines=['СЛУЖБЫ НА ЭТОЙ VPS','Снимок на момент открытия. Это не проверка VPN в клиенте.','']
        failed=[]
        for unit,name in units.items():
            values=states.get(unit,{})
            if values.get('LoadState')=='not-found': status='не настроено'
            elif values.get('ActiveState')=='failed' or values.get('Result','success') not in ('','success'):
                status='FAILED — ошибка службы'; failed.append(name)
            elif values.get('ActiveState')=='active':
                status='запуск по расписанию включён' if unit.endswith('.timer') else (
                       'выполнено' if values.get('SubState')=='exited' else 'работает')
            elif (unit=='remna-hy2-certs.service' and values.get('ActiveState')=='inactive'
                  and states.get('remna-hy2-certs.timer',{}).get('ActiveState')=='active'):
                status='ожидает запуска по таймеру'
            else:
                status={'inactive':'остановлена','activating':'запускается','deactivating':'останавливается',
                        'reloading':'перезагружается'}.get(values.get('ActiveState'),'статус недоступен')
            lines.append(name+': '+status)
        lines+=['','Не настроено — допустимо для невыбранных протоколов и SSH.']
        if failed: lines+=['','Откройте журнал служб со сбоем: '+', '.join(failed)+'.']
        if not states: lines+=['','Ошибка чтения systemd: '+result.stdout.strip()]
        return '\n'.join(lines)


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
            app.say('Дополнительные подключения для вашей ноды.','muted')
            in_menu=True
            try:
                if first and argv: selected='setup'
                else:
                    selected=app.select('Выберите действие',[
                        ('setup','Протоколы и доступ'),
                        ('network','Yggdrasil и SSH'),
                        ('result','Мои подключения'),('logs','Диагностика и журналы'),('exit','Выход')],'setup')
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
        app.finish(); app.environment.clear(); app.secrets.clear(); ui.screen=None
        window.keypad(False); curses.nocbreak(); curses.echo(); curses.endwin()
        if interrupted: print('Применение прервано. Часть шагов могла завершиться; журнал: '+str(LOG_DIR/'installer.log'))
