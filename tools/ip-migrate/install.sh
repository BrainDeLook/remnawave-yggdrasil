#!/usr/bin/env bash
set -euo pipefail

# Standalone tool. Does not install or launch the RemnaYgg node installer.
if [[ $(id -u) != 0 ]]; then
    printf 'Запустите от root на VPS панели Remnawave.\n' >&2
    exit 1
fi
for command in curl python3 sha256sum; do
    if ! command -v "$command" >/dev/null 2>&1; then
        printf 'Не найден %s. Нужны curl, python3 и ca-certificates.\n' "$command" >&2
        exit 1
    fi
done

work=$(mktemp -d /tmp/remna-ip-migrate.XXXXXXXX)
trap 'rm -rf -- "$work"' EXIT
source_url='https://raw.githubusercontent.com/BrainDeLook/remnawave-yggdrasil/main/tools/ip-migrate/migrate.py'
expected='f532066410dadcd4178e77759024a74178dfbbed8d3fe3763c450ad6e2ce1361'
printf 'Загружаю инструмент смены IP панели Remnawave…\n'
curl --proto '=https' --tlsv1.2 -fsSL --connect-timeout 15 --max-time 90 "$source_url" -o "$work/migrate.py"
actual=$(sha256sum "$work/migrate.py")
if [[ ${actual%% *} != "$expected" ]]; then
    printf 'Контрольная сумма не совпала. Скачайте свежий загрузчик и повторите.\n' >&2
    exit 1
fi
python3 "$work/migrate.py" "$@"
