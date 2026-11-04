"""Only UI actions and temporary files; no network or system configuration."""
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import screen
import ui

root=Path(sys.argv[1]); screen.LOG_DIR=root/'logs'
def fixture(argv):
    ui.section(1,'Панель')
    panel=ui.prompt('Адрес панели')
    ui.section(2,'Авторизация')
    token=ui.secret('API-токен')
    assert token=='PTY_PRIVATE_TOKEN'
    ui.section(3,'Выбор протоколов')
    ui.multi_select('Дополнения',[('ygg','Yggdrasil')],[],allow_empty=False)
    ui.section(4,'План')
    ui.say('До подтверждения файлы не создаются.')
    if ui.confirm('Применить план?'):
        ui.begin_apply()
        ui.section(1,'Применение')
        ui.say('Применение завершено.','success')
        (root/'applied').write_text(panel)
        ui.page('Готово'); ui.say('Готово','success')

assert screen.launch(fixture,lambda:None,'test',[])
