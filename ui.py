"""Consistent terminal presentation; secrets never pass through display helpers."""
from __future__ import annotations
import getpass
import os
import shutil
import sys
import textwrap
from typing import Literal
from api import Error
import node as local

screen = None


def fullscreen() -> bool:
    return screen is not None


def env_secret(name: str) -> str:
    if screen is not None:
        if name not in screen.environment:
            screen.environment[name] = os.environ.pop(name, '')
        return screen.environment[name]
    return os.environ.pop(name, '')


def begin_apply():
    if screen is not None: screen.begin_apply()


def page(title: str):
    if screen is not None: screen.page(title)

Tone = Literal['text','muted','section','question','value','success','example','warning','error']
COLORS = {'text':'0','muted':'90','section':'1;36','question':'1;33','value':'1;32',
          'success':'1;32','example':'1;34','warning':'38;5;214','error':'1;31'}


def paint(text: str, tone: Tone = 'text') -> str:
    enabled='NO_COLOR' not in os.environ and os.environ.get('TERM')!='dumb' and (sys.stdout.isatty() or os.environ.get('FORCE_COLOR')=='1')
    return '\033['+COLORS[tone]+'m'+text+'\033[0m' if enabled else text


def say(text: str = '', tone: Tone = 'text'):
    if screen is not None:
        screen.say(text,tone); return
    print(paint(text,tone),flush=True)


def section(number: int, title: str):
    if screen is not None:
        screen.page((str(number)+' · ' if number else '')+title); return
    say('\n  ━━ '+str(number)+' · '+title+' ━━','section')


def field(label: str, value: str):
    if screen is not None:
        screen.say(label+': '+value,'value'); return
    print(paint('  '+label+': ','muted')+paint(value,'value'),flush=True)


def option(number: int, title: str, detail: str = ''):
    if screen is not None:
        screen.say(f'{number}. {title}'+(' — '+detail if detail else '')); return
    print(paint(f'    {number}. ','section')+paint(title,'text')+
          (paint(' — '+detail,'muted') if detail else ''),flush=True)


def panel(title: str, lines: list[tuple[str,Tone]], tone: Tone = 'section'):
    if screen is not None:
        screen.say(title,tone)
        for text,line_tone in lines: screen.say(text,line_tone)
        return
    width=max(40,min(88,shutil.get_terminal_size((88,24)).columns-4))
    say('\n  ┌'+'─'*(width-2)+'┐',tone)
    for line,line_tone in [(title,tone),*lines]:
        for wrapped in textwrap.wrap(line,width-6,replace_whitespace=False) or ['']:
            print(paint('  │ ',tone)+paint(wrapped,line_tone)+' '*(width-3-len(wrapped))+paint('│',tone),flush=True)
    say('  └'+'─'*(width-2)+'┘',tone)


def prompt(label: str, default: str = '') -> str:
    if screen is not None: return screen.prompt(label,default)
    say('\n  '+label,'question')
    if default:
        print(paint('  Enter → ','muted')+paint(default,'value'),flush=True)
    return input(paint('  > ','question')).strip() or default


def secret(label: str) -> str:
    if screen is not None: return screen.prompt(label,'',secret=True)
    say('\n  '+label,'question')
    say('  Ввод скрыт: символы и звёздочки не отображаются. Вставьте значение и нажмите Enter.','muted')
    return getpass.getpass(paint('  > ','question')).strip()


def confirm(label: str, default: bool = False) -> bool:
    if screen is not None:
        return screen.select(label,[('yes','Да'),('no','Нет')],'yes' if default else 'no')=='yes'
    while True:
        value=prompt(label+' [да/нет]','да' if default else 'нет').lower()
        if value in ('да','yes','y','д'): return True
        if value in ('нет','no','n','н'): return False
        say('  Введите «да» или «нет». Можно переключить раскладку и повторить ввод.','error')


def choose(items: list, key: str | None, saved: str | None, yes: bool, label: str):
    if not items: raise Error('Нет доступных вариантов: '+label)
    selected=next((v for v in items if v.uuid==(key or saved)),None)
    if key and selected is None: raise Error('Указанный UUID не найден: '+label)
    if screen is not None and not key and not yes:
        value=screen.select(label,[(v.uuid,v.name) for v in items],selected.uuid if selected else items[0].uuid)
        selected=next(v for v in items if v.uuid==value)
    if selected is None and len(items)==1: selected=items[0]
    if selected is None:
        if yes: raise Error('Найдено несколько вариантов. Укажите UUID: '+label)
        say('\n  '+label,'question')
        for index,item in enumerate(items,1): option(index,item.name)
        while selected is None:
            value=prompt('Введите номер из списка')
            if value.isdigit() and 1<=int(value)<=len(items): selected=items[int(value)-1]
            else: say('  Введите один номер из списка выше.','error')
    field(label,selected.name)
    return selected


def multi_select(label: str, choices: list[tuple[str,str]], selected: list[str], allow_empty: bool = True) -> list[str]:
    if screen is not None: return screen.select(label,choices,selected,multiple=True,allow_empty=allow_empty)
    for number,(_,name) in enumerate(choices,1): option(number,name)
    default=' '.join(str(i) for i,(key,_) in enumerate(choices,1) if key in selected)
    while True:
        answer=prompt(label,default).split()
        if not answer and allow_empty: return []
        if answer and all(v.isdigit() and 1<=int(v)<=len(choices) for v in answer):
            return list(dict.fromkeys(choices[int(v)-1][0] for v in answer))
        say('  Выберите номера из списка через пробел.','error')


def access(values: list[str], old: list[str], yes: bool, label: str, role: str = 'any') -> list[str]:
    if values: return local.networks(values,role=role)
    if yes:
        if not old: raise Error('Не задан список доступа: '+label)
        return local.networks(old,role=role)
    while True:
        value=prompt(label,' '.join(old))
        try: return local.networks(value.split(),role=role)
        except Error as exc: say('  '+str(exc),'error')
