"""Read core identity and owned listeners without trusting a host-wide open port."""
from dataclasses import dataclass
import re
import time
from api import Error
from node import run

# Fixed read-only script: no user values or Docker environment enter its output.
CORE_PROBE = r'''for p in /proc/[0-9]*; do
    IFS= read -r name < "$p/comm" 2>/dev/null || continue
    case "$name" in rw-core|xray) ;; *) continue ;; esac
    effective=; filesystem=
    if ! { while read -r field real current saved fs rest; do
        if [ "$field" = "Uid:" ]; then effective=$current; filesystem=$fs; break; fi
    done < "$p/status"; } 2>/dev/null; then continue; fi
    [ -d "$p" ] || continue
    printf 'CORE %s %s %s\n' "${p##*/}" "${effective:--}" "${filesystem:--}"
    for fd in "$p"/fd/*; do
        target=$(readlink "$fd" 2>/dev/null) || continue
        case "$target" in
            socket:\[*\]) inode=${target#socket:[}; inode=${inode%]}; printf 'FD %s %s\n' "${p##*/}" "$inode" ;;
        esac
    done
done
for protocol in tcp tcp6 udp udp6; do
    if [ -r "/proc/net/$protocol" ]; then
        printf 'TABLE %s\n' "$protocol"
        cat "/proc/net/$protocol"
    fi
done
'''
STARTUP_TIMEOUT = 60


@dataclass(frozen=True)
class Listener:
    protocol: str
    address: str
    port: int


@dataclass(frozen=True)
class Core:
    effective_uid: int
    filesystem_uid: int
    listeners: frozenset[Listener]


def core_snapshot() -> Core | None:
    """Validate /proc output once; a missing core is expected during a restart."""
    output=run('docker','exec','remnanode','sh','-c',CORE_PROBE,check=False,timeout=10)
    identities=[]; owned=set(); sockets=[]; protocol=None
    for line in output.splitlines():
        fields=line.split()
        if not fields: continue
        if fields[0]=='CORE':
            if len(fields)!=4 or not all(value.isdecimal() for value in fields[1:]):
                raise Error('Не удалось прочитать UID процесса rw-core/Xray')
            identities.append(tuple(map(int,fields[1:])))
        elif fields[0]=='FD':
            if len(fields)!=3 or not all(value.isdecimal() for value in fields[1:]):
                raise Error('Не удалось проверить сокеты процесса rw-core/Xray')
            owned.add((int(fields[1]),fields[2]))
        elif fields[0]=='TABLE':
            if len(fields)!=2 or fields[1] not in ('tcp','tcp6','udp','udp6'):
                raise Error('Не удалось прочитать таблицу сокетов rw-core/Xray')
            protocol=fields[1]
        elif protocol and fields[0]=='sl':
            continue
        elif protocol:
            if len(fields)<10 or not re.fullmatch(r'[0-9A-Fa-f]{8}(?:[0-9A-Fa-f]{24})?:[0-9A-Fa-f]{4}',fields[1]) or not fields[9].isdecimal():
                raise Error('Некорректная таблица сокетов rw-core/Xray')
            if fields[3] == ('0A' if protocol.startswith('tcp') else '07'):
                address,port=fields[1].split(':')
                sockets.append((fields[9],Listener(protocol,address.upper(),int(port,16))))
        else:
            raise Error('Не удалось проверить процесс rw-core/Xray')
    if not identities: return None
    if len(identities)!=1:
        raise Error('Найдено несколько процессов rw-core/Xray; автоматическая проверка неоднозначна')
    pid,effective,filesystem=identities[0]
    listeners=frozenset(listener for inode,listener in sockets if (pid,inode) in owned)
    return Core(effective,filesystem,listeners)


def require_root_core() -> None:
    core=core_snapshot()
    if core is None:
        raise Error('Процесс rw-core/Xray не найден; запустите ноду перед настройкой сертификата')
    if core.effective_uid!=0 or core.filesystem_uid!=0:
        raise Error('Процесс rw-core/Xray работает не от root; ключ 600 ему недоступен. Настройте владельца/группу вручную; права 644 не применяются.')


def restart_and_wait(udp_ports: tuple[int,...] = (), *,
                     saved_listeners: frozenset[Listener] = frozenset()) -> None:
    before=core_snapshot()
    if before is None:
        # tmpfs is empty after boot, so Xray may be unable to start until its
        # certificates are restored. Only previously root-verified listeners,
        # stored in the private configuration, may replace a live snapshot.
        if not saved_listeners:
            raise Error('Процесс rw-core/Xray не найден и сохранённые слушатели отсутствуют; повторите настройку на работающей ноде')
        expected=saved_listeners
    else:
        if before.effective_uid!=0 or before.filesystem_uid!=0:
            raise Error('Процесс rw-core/Xray работает не от root; ключ остаётся защищённым правами 600')
        # Outgoing UDP sockets have transient ports. Only selected inbound ports
        # are relevant; a first install has no Hysteria until profile apply.
        # A failed previous restart can leave a partial live core. Do not lose
        # verified expectations merely because that partial core is visible now.
        expected=saved_listeners | frozenset(listener for listener in before.listeners
                                             if listener.protocol.startswith('tcp') or listener.port in udp_ports)
        if not expected:
            raise Error('Не удалось определить слушатели rw-core/Xray перед перезапуском')
    run('docker','restart','remnanode',timeout=120)
    deadline=time.monotonic()+STARTUP_TIMEOUT
    while time.monotonic()<deadline:
        current=core_snapshot()
        if current is not None:
            if current.effective_uid!=0 or current.filesystem_uid!=0:
                raise Error('После перезапуска rw-core/Xray работает не от root; ключ остаётся защищённым правами 600')
            if expected <= current.listeners: return
        time.sleep(2)
    raise Error('После обновления сертификата слушатели rw-core/Xray не восстановились за 60 секунд. Проверьте docker logs remnanode; повторный запуск не ослабит права ключа.')
