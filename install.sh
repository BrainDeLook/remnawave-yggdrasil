#!/usr/bin/env bash
set -Eeuo pipefail
umask 077
VERSION=v0.2.4
BUNDLE_SHA256=c5a021269ec133ef3c8c44d5b5e0142a8b4d4369f40b17beb2d4ed56f3955a9d
URL="https://github.com/BrainDeLook/remnawave-yggdrasil/releases/download/$VERSION/remnawave-yggdrasil-$VERSION.tar"
color_section='' color_text='' color_error='' color_reset=''
if [[ -t 1 && ${TERM:-} != dumb && ! -v NO_COLOR ]]; then
    color_section=$'\033[1;36m' color_text=$'\033[0m' color_error=$'\033[1;31m' color_reset=$'\033[0m'
fi
if [[ ${1:-} == --help || ${1:-} == -h ]]; then
    printf '%s\n' 'Remnawave + Yggdrasil: запуск на VPS действующей ноды.' \
      'sudo bash install.sh  — цветной мастер' \
      'sudo bash install.sh --check  — только проверка API и план' \
      'Протоколы: Hysteria2, VLESS gRPC, VLESS XHTTP, VLESS через Yggdrasil.' \
      'API-токен вводится скрыто и не сохраняется.'
    exit 0
fi
[[ $EUID -eq 0 ]] || { printf '%sЗапустите установщик от root: sudo bash install.sh%s\n' "$color_error" "$color_reset" >&2; exit 1; }
export DEBIAN_FRONTEND=noninteractive LC_ALL=C
if ! command -v python3 >/dev/null || ! command -v curl >/dev/null || [[ ! -s /etc/ssl/certs/ca-certificates.crt ]]; then
    command -v apt-get >/dev/null || { echo 'Нужен Debian/Ubuntu с APT'; exit 1; }
    install -d -m 700 /var/log/remnawave-yggdrasil
    printf '%sПодготовка загрузчика%s\n' "$color_section" "$color_reset"
    printf 'Устанавливаю Python, curl и сертификаты доверия. Это может занять несколько минут.\nЖурнал: /var/log/remnawave-yggdrasil/bootstrap.log\n'
    apt-get update >/var/log/remnawave-yggdrasil/bootstrap.log 2>&1
    apt-get install -y python3 curl ca-certificates >>/var/log/remnawave-yggdrasil/bootstrap.log 2>&1
fi
scratch=$(mktemp -d /tmp/remna-ygg-bootstrap.XXXXXXXX)
trap 'rm -rf -- "$scratch"' EXIT
printf '\n%sRemnawave · дополнительные протоколы · %s%s\n' "$color_section" "$VERSION" "$color_reset"
printf 'Загружаю проверенную сборку. Затем откроется мастер настройки.\n'
curl -fsSL --retry 3 --connect-timeout 20 --max-time 180 --proto '=https' --proto-redir '=https' "$URL" -o "$scratch/bundle.tar"
printf '%s  %s\n' "$BUNDLE_SHA256" "$scratch/bundle.tar" | sha256sum -c --status || { printf '%sКонтрольная сумма не совпала. Установка остановлена.%s\n' "$color_error" "$color_reset"; exit 1; }
tar -xf "$scratch/bundle.tar" -C "$scratch" main.py api.py node.py probe.py multi.py converter.py provision.py certificates.py ui.py
python3 "$scratch/multi.py" "$@"
