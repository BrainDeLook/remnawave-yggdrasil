"""Native offline Windows desktop converter. Only Python's standard GUI toolkit."""
import argparse
import json
from pathlib import Path
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from tkinter.scrolledtext import ScrolledText
from converter import Profile, convert, LABELS


def main():
    parser=argparse.ArgumentParser(); parser.add_argument('--self-test',action='store_true'); args=parser.parse_args()
    root=tk.Tk(); root.title('Remnawave · локальный конвертер 0.2.0')
    root.geometry('1120x800'); root.minsize(900,680)
    root.configure(bg='#131d2b')
    style=ttk.Style(); style.theme_use('clam')
    style.configure('.',font=('Segoe UI',10),background='#131d2b',foreground='#e6edf7')
    style.configure('TButton',padding=8,background='#244365')
    style.configure('TCheckbutton',padding=5)
    style.map('TCheckbutton',background=[('active','#244365')])
    style.configure('Title.TLabel',font=('Segoe UI',19,'bold'),foreground='#63d9bd')
    shell=ttk.Frame(root,padding=20); shell.pack(fill='both',expand=True)
    ttk.Label(shell,text='Дополнительные протоколы Remnawave',style='Title.TLabel').pack(anchor='w')
    ttk.Label(shell,text='Полностью локально · без сервера, браузера, API-токена и отправки конфигурации в сеть').pack(anchor='w',pady=(4,12))
    ttk.Label(shell,text='Основной установщик уже добавляет протоколы через API. Этот конвертер — для ручного редактирования профиля.\n1. Откройте JSON профиля.  2. Выберите базовый inbound и дополнения.  3. Проверьте и сохраните новый JSON.').pack(anchor='w')
    row=ttk.Frame(shell); row.pack(fill='x',pady=10)
    source_path=tk.StringVar(value='Файл не выбран')
    status=tk.StringVar(value='Исходный файл не перезаписывается. Результат содержит приватный ключ — храните как пароль.')
    base=tk.StringVar(); ygg_port=tk.StringVar(value='1488')
    selected={p:tk.BooleanVar(value=p=='ygg') for p in LABELS}
    editor=ScrolledText(shell,height=11,font=('Consolas',10),bg='#0c1420',fg='#edf4ff',insertbackground='white',wrap='none',undo=True)
    output=ScrolledText(shell,height=10,font=('Consolas',10),bg='#0c1420',fg='#a6e6d1',wrap='none')
    output.configure(state='disabled')
    combo=None
    current_file=None

    def refresh_bases():
        profile=Profile.parse(json.loads(editor.get('1.0','end')))
        combo['values']=profile.bases
        if base.get() not in profile.bases: base.set(profile.bases[0] if profile.bases else '')
        if not profile.bases: raise ValueError('В профиле нет базового VLESS TCP/RAW Reality inbound')
        return profile

    def load():
        nonlocal current_file
        filename=filedialog.askopenfilename(filetypes=[('JSON профиля','*.json'),('Все файлы','*.*')])
        if not filename: return
        content=Path(filename).read_text(encoding='utf-8-sig')
        Profile.parse(json.loads(content))
        editor.delete('1.0','end'); editor.insert('1.0',content)
        current_file=Path(filename).resolve(); source_path.set(filename); refresh_bases()
        status.set('Выберите дополнения. Все прежние inbound и разделы профиля будут сохранены.')

    def generate():
        profile=refresh_bases()
        result,mapping=convert(profile,base.get(),[p for p,v in selected.items() if v.get()],{'ygg':int(ygg_port.get())})
        output.configure(state='normal'); output.delete('1.0','end')
        output.insert('1.0',json.dumps(result,ensure_ascii=False,indent=2)); output.configure(state='disabled')
        status.set('Готово: '+', '.join(mapping.values())+'. Дальше: сохранить JSON и применить в профиле Remnawave.')

    def save():
        content=output.get('1.0','end').strip()
        if not content: raise ValueError('Сначала нажмите «Преобразовать»')
        filename=filedialog.asksaveasfilename(defaultextension='.json',initialfile='remnawave-multi.json',filetypes=[('JSON профиля','*.json')])
        if not filename: return
        if current_file and Path(filename).resolve()==current_file: raise ValueError('Выберите другое имя: исходный файл сохраняем')
        Path(filename).write_text(content+'\n',encoding='utf-8')
        status.set('Сохранено. В панели примените JSON, активируйте новые inbound на ноде, создайте Host и назначьте сквадам.')

    def callback_error(kind,value,traceback):
        messagebox.showerror('Проверьте ввод',str(value),parent=root)
    root.report_callback_exception=callback_error
    ttk.Button(row,text='1 · Открыть JSON…',command=load).pack(side='left')
    ttk.Label(row,textvariable=source_path).pack(side='left',padx=12)
    ttk.Label(shell,text='Исходный профиль — можно открыть файл или вставить JSON сюда:').pack(anchor='w')
    editor.pack(fill='both',expand=True,pady=(4,8))
    options=ttk.Frame(shell); options.pack(fill='x')
    ttk.Label(options,text='Базовый TCP Reality:').pack(side='left')
    combo=ttk.Combobox(options,textvariable=base,state='readonly',width=35); combo.pack(side='left',padx=8)
    ttk.Button(options,text='Прочитать вставленный JSON',command=refresh_bases).pack(side='left')
    checks=ttk.Frame(shell); checks.pack(fill='x',pady=8)
    for p,label in LABELS.items(): ttk.Checkbutton(checks,text=label,variable=selected[p]).pack(side='left')
    actions=ttk.Frame(shell); actions.pack(fill='x',pady=(0,8))
    ttk.Label(actions,text='Порт Yggdrasil:').pack(side='left')
    ttk.Entry(actions,textvariable=ygg_port,width=7).pack(side='left',padx=(5,12))
    ttk.Button(actions,text='2 · Преобразовать',command=generate).pack(side='left',padx=5)
    ttk.Button(actions,text='3 · Сохранить результат…',command=save).pack(side='left',padx=5)
    ttk.Label(shell,text='Результат: существующие inbound сохраняются; новые имеют отдельные tag и порты. dest/SNI Reality сохраняются.').pack(anchor='w')
    output.pack(fill='both',expand=True,pady=(4,8))
    ttk.Label(shell,textvariable=status,wraplength=1040,foreground='#f5cc7a').pack(anchor='w')
    ttk.Label(shell,text='Yggdrasil требует подготовки VPS установщиком. В Тахионе может требоваться привязка интерфейса на роутере.',foreground='#91aac9').pack(anchor='w',pady=(8,0))
    if args.self_test:
        root.withdraw()
        fixture={'inbounds':[{'tag':'BASE','port':443,'protocol':'vless','settings':{'clients':[]},
                 'streamSettings':{'network':'tcp','security':'reality','realitySettings':{
                     'privateKey':'test','serverNames':['example.com'],'shortIds':['abcd'],'dest':'/dev/shm/nginx.sock'}}}]}
        editor.insert('1.0',json.dumps(fixture)); generate()
        assert len(json.loads(output.get('1.0','end'))['inbounds'])==2
        root.update_idletasks(); root.destroy(); return
    root.mainloop()

if __name__=='__main__': main()
