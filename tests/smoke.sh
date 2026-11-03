#!/usr/bin/env bash
# Disposable CI VM only: never run this test on a real node.
set -Eeuo pipefail
export DEBIAN_FRONTEND=noninteractive LC_ALL=C
ROOT=$(cd -- "$(dirname -- "$0")/.." && pwd)
cd "$ROOT"
apt-get update
apt-get install -y python3 curl ca-certificates openssl iproute2 nftables ufw
mkdir -p .test-bin
curl -fsSL --retry 3 https://github.com/yggdrasil-network/yggdrasil-go/releases/download/v0.5.14/yggdrasil-0.5.14-amd64.deb -o .test-bin/ygg.deb
echo '72745496b09baa1c3f5a6741e1257a704cfdb9d00cdf3ddbef025987b239699d  .test-bin/ygg.deb' | sha256sum -c -
dpkg-deb -x .test-bin/ygg.deb .test-bin/ygg
curl -fsSL --retry 3 https://github.com/XTLS/Xray-core/releases/download/v26.10.21/Xray-linux-64.zip -o .test-bin/xray.zip
echo '0eec3ed4163c273d8c8bf821f7d219e257921cf584aa3fb970715e458e631978  .test-bin/xray.zip' | sha256sum -c -
python3 -m zipfile -e .test-bin/xray.zip .test-bin/xray
chmod +x .test-bin/xray/xray
export REMNA_YGG_XRAY="$ROOT/.test-bin/xray/xray"
python3 tests/real_stack.py
