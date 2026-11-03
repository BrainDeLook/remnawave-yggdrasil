#!/usr/bin/env bash
set -Eeuo pipefail
umask 077
VERSION=v0.2.1
BUNDLE_SHA256=382512163564a4df1311b635d59811b4afbfdbdf100ad829e18e02bdbf8888dc
URL="https://github.com/BrainDeLook/remnawave-yggdrasil/releases/download/$VERSION/remnawave-yggdrasil-$VERSION.tar"
if [[ ${1:-} == --help || ${1:-} == -h ]]; then
    printf '%s\n' 'Remnawave + Yggdrasil: запуск на VPS действующей ноды.' \
      'sudo bash install.sh  — цветной мастер' \
      'sudo bash install.sh --check  — только проверка API и план' \
      'Протоколы: Hysteria2, VLESS gRPC, VLESS XHTTP, VLESS через Yggdrasil.' \
      'API-токен вводится скрыто и не сохраняется.'
    exit 0
fi
[[ $EUID -eq 0 ]] || { echo 'Запустите от root: sudo bash install.sh' >&2; exit 1; }
export DEBIAN_FRONTEND=noninteractive LC_ALL=C
if ! command -v python3 >/dev/null || ! command -v curl >/dev/null || [[ ! -s /etc/ssl/certs/ca-certificates.crt ]]; then
    command -v apt-get >/dev/null || { echo 'Нужен Debian/Ubuntu с APT'; exit 1; }
    install -d -m 700 /var/log/remnawave-yggdrasil
    echo 'Подготавливаю Python и загрузчик. Журнал: /var/log/remnawave-yggdrasil/bootstrap.log'
    apt-get update >/var/log/remnawave-yggdrasil/bootstrap.log 2>&1
    apt-get install -y python3 curl ca-certificates >>/var/log/remnawave-yggdrasil/bootstrap.log 2>&1
fi
scratch=$(mktemp -d /tmp/remna-ygg-bootstrap.XXXXXXXX)
trap 'rm -rf -- "$scratch"' EXIT
printf '\nЗагружаю Remnawave + Yggdrasil %s…\n' "$VERSION"
curl -fsSL --retry 3 --connect-timeout 20 --max-time 180 --proto '=https' --proto-redir '=https' "$URL" -o "$scratch/bundle.tar"
printf '%s  %s\n' "$BUNDLE_SHA256" "$scratch/bundle.tar" | sha256sum -c --status || { echo 'Контрольная сумма не совпала'; exit 1; }
tar -xf "$scratch/bundle.tar" -C "$scratch" main.py api.py node.py probe.py multi.py converter.py provision.py certificates.py
python3 "$scratch/multi.py" "$@"
