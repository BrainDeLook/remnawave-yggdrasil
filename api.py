"""Small typed boundary for the Remnawave API. No credentials are persisted."""
from __future__ import annotations
import copy
import ipaddress
import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any
from uuid import UUID

class Error(RuntimeError):
    pass

def obj(value: Any) -> dict:
    if not isinstance(value, dict):
        raise Error('Неожиданный формат API: ожидался объект. Нужна Remnawave 3.x.')
    return value

def string(data: dict, key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value:
        raise Error(f'API: отсутствует строковое поле {key}')
    return value

def uid(data: dict, key: str) -> str:
    value = string(data, key)
    try:
        return str(UUID(value))
    except ValueError as exc:
        raise Error(f'API: некорректный UUID в {key}') from exc

def seq(value: Any) -> list:
    if not isinstance(value, list):
        raise Error('API: ожидался список')
    return value

@dataclass(frozen=True)
class Inbound:
    uuid: str
    tag: str

@dataclass(frozen=True)
class Node:
    uuid: str
    name: str
    address: str
    profile: str
    inbounds: tuple[Inbound, ...]

    @classmethod
    def parse(cls, value: Any) -> Node:
        d = obj(value)
        profile = obj(d.get('configProfile'))
        if not d.get('isConnected') or d.get('isDisabled'):
            raise Error('Выбранная нода отключена или не подключена к панели')
        return cls(uid(d, 'uuid'), string(d, 'name'), string(d, 'address'),
                   uid(profile, 'activeConfigProfileUuid'),
                   tuple(Inbound(uid(obj(i), 'uuid'), string(obj(i), 'tag'))
                         for i in seq(profile.get('activeInbounds'))))

@dataclass(frozen=True)
class Host:
    uuid: str
    name: str
    address: str
    port: int
    profile: str
    inbound: str
    nodes: tuple[str, ...]
    raw: dict

    @classmethod
    def parse(cls, value: Any) -> Host:
        d = obj(value)
        binding = obj(d.get('inbound'))
        port = d.get('port')
        if type(port) is not int or not 1 <= port <= 65535:
            raise Error('API: некорректный порт Host')
        return cls(uid(d, 'uuid'), string(d, 'remark'), string(d, 'address'), port,
                   uid(binding, 'configProfileUuid'), uid(binding, 'configProfileInboundUuid'),
                   tuple(str(UUID(v)) for v in seq(d.get('nodes', []))), d)

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None

class API:
    def __init__(self, url: str, token: str):
        parsed = urllib.parse.urlsplit(url.rstrip('/'))
        if parsed.scheme != 'https' and not (parsed.scheme == 'http' and parsed.hostname in ('localhost', '127.0.0.1', '::1')):
            raise Error('URL панели должен использовать HTTPS; HTTP разрешён только на localhost')
        if not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in ('', '/', '/api', '/api/'):
            raise Error('Укажите корневой URL панели, без логина, пути страницы и параметров')
        if not token or any(c in token for c in '\r\n'):
            raise Error('Пустой или некорректный API-токен')
        self.url = urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, '/api', '', ''))
        self.local = parsed.hostname in ("localhost", "127.0.0.1", "::1")
        self.token = token
        self.opener = urllib.request.build_opener(NoRedirect)

    def request(self, method: str, path: str, body: dict | None = None) -> Any:
        payload = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.url + path, payload, method=method,
              headers={'Authorization': 'Bearer ' + self.token, 'Content-Type': 'application/json',
                       **({'X-Forwarded-For': '127.0.0.1'} if self.local else {}),
                       'X-Forwarded-Proto': 'https', 'User-Agent': 'remnawave-yggdrasil/0.1.0'})
        try:
            with self.opener.open(req, timeout=30) as response:
                data = json.loads(response.read(8 * 1024 * 1024))
        except urllib.error.HTTPError as exc:
            # Response bodies may contain secrets. Do not echo them or the token.
            code=exc.code
            exc.close()
            raise Error(f'API {method} {path.split("?")[0]}: HTTP {code}. Проверьте права токена и доступ к API через reverse proxy.') from None
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise Error('API недоступен, вернул не JSON или истёк тайм-аут. TLS-проверка не отключается.') from None
        data=obj(data)
        if 'response' not in data: raise Error('Неожиданный формат API: отсутствует response')
        return data['response']

    def nodes(self) -> list[Node]:
        # Ignore disconnected nodes at this input boundary; an explicitly selected one still fails.
        return [Node.parse(d) for value in seq(self.request('GET', '/nodes'))
                if (d := obj(value)).get('isConnected') and not d.get('isDisabled')]

    def hosts(self) -> list[Host]:
        # Unbound/disabled hosts are not candidates for a native endpoint.
        return [Host.parse(d) for value in seq(self.request('GET', '/hosts'))
                if (d := obj(value)).get('inbound', {}).get('configProfileInboundUuid')
                ]

    def inbound(self, node: Node, host: Host) -> dict:
        profile = obj(self.request('GET', '/config-profiles/' + node.profile))
        config = obj(profile.get('config'))
        metadata = seq(profile.get('inbounds'))
        tag = next((string(obj(v), 'tag') for v in metadata if obj(v).get('uuid') == host.inbound), None)
        raw = next((obj(v) for v in seq(config.get('inbounds')) if obj(v).get('tag') == tag), None)
        if raw is None:
            raise Error('Inbound Host не найден в активном профиле выбранной ноды')
        stream = obj(raw.get('streamSettings'))
        if raw.get('protocol') != 'vless' or stream.get('security') != 'reality' or stream.get('network', 'tcp') not in ('tcp', 'raw'):
            raise Error('Первая версия поддерживает существующий VLESS TCP/RAW Reality inbound')
        if type(raw.get('port')) is not int or raw['port'] != host.port:
            raise Error('Host использует перенаправление порта; выберите Host с портом самого inbound')
        reality = obj(stream.get('realitySettings'))
        names = seq(reality.get('serverNames'))
        if not names or not all(isinstance(v, str) and v for v in names):
            raise Error('В Reality inbound отсутствует serverNames')
        if not isinstance(reality.get('privateKey'), str) or not reality['privateKey']:
            raise Error('Нужен обычный Reality privateKey в профиле; ссылка на env/внешний секрет пока не поддерживается')
        return raw

COPY_FIELDS = ('path', 'host', 'alpn', 'fingerprint', 'securityLayer', 'xhttpExtraParams',
               'muxParams', 'sockoptParams', 'finalMask', 'serverDescription', 'internalSquads',
               'xrayJsonTemplateUuid', 'excludeFromSubscriptionTypes', 'mihomoX25519',
               'pinnedPeerCertSha256', 'verifyPeerCertByName', 'vlessRouteId')

def host_payload(node: Node, source: Host, raw: dict, address: str, name: str) -> dict:
    if source.raw.get('mapper') and any(source.raw['mapper'].values()):
        raise Error('Host с пользовательским mapper требует отдельной проверки. Выберите обычный VLESS Reality Host.')
    reality = raw['streamSettings']['realitySettings']
    sni = source.address if source.raw.get('overrideSniFromAddress') else source.raw.get('sni') or reality['serverNames'][0]
    # Client SNI must not accidentally become the Yggdrasil connection address.
    if sni not in reality['serverNames']:
        raise Error('SNI исходного Host не входит в serverNames inbound')
    if not 1 <= len(name) <= 100 or any(c in name for c in '\r\n'):
        raise Error('Название Host должно содержать от 1 до 100 символов без переноса строки')
    result = {k: copy.deepcopy(source.raw[k]) for k in COPY_FIELDS if k in source.raw and source.raw[k] is not None}
    result.update(inbound={'configProfileUuid': node.profile, 'configProfileInboundUuid': source.inbound},
                  remark=name, address=str(ipaddress.IPv6Address(address)), port=source.port,
                  sni=sni, overrideSniFromAddress=False, keepSniBlank=False,
                  isDisabled=False, isHidden=False, nodes=[node.uuid],
                  tags=['YGGDRASIL_NATIVE'], shuffleHost=False)
    return result

def eligible(node: Node, host: Host) -> bool:
    return not host.raw.get('isDisabled') and not host.raw.get('isHidden') and host.profile == node.profile and host.inbound in {i.uuid for i in node.inbounds} and (not host.nodes or node.uuid in host.nodes) and 'YGGDRASIL_NATIVE' not in host.raw.get('tags', [])

def upsert(api: API, payload: dict, hosts: list[Host], saved_uuid: str | None) -> Host:
    matches = [h for h in hosts if (h.uuid == saved_uuid if saved_uuid else
               'YGGDRASIL_NATIVE' in h.raw.get('tags', []) and h.address == payload['address']
               and h.nodes == tuple(payload['nodes']) and h.inbound == payload['inbound']['configProfileInboundUuid'])]
    if saved_uuid and not matches:
        # A manually deleted endpoint may be recreated, but never silently overwrite another host.
        matches = []
    if len(matches) > 1:
        raise Error('Найдено несколько наших Host. Удалите дубликат в панели и повторите запуск.')
    if matches:
        current = matches[0]
        if 'YGGDRASIL_NATIVE' not in current.raw.get('tags', []) or current.nodes != tuple(payload['nodes']):
            raise Error('Сохранённый UUID больше не принадлежит Host этого установщика')
        if all(current.raw.get(k) == v for k, v in payload.items()):
            return current
        return Host.parse(api.request('PATCH', '/hosts', dict(payload, uuid=current.uuid)))
    # No write retry: a timed-out POST can have succeeded. Next invocation reconciles by tag/address.
    return Host.parse(api.request('POST', '/hosts', payload))
