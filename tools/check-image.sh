#!/usr/bin/env bash
#
# Check a built GhostStack image: no pip or pip-vendored CA bundle, no trust in
# the distrusted emSign ECC Root CA - G3 (in the OS store or in the store httpx
# loads at runtime), and the server still boots without pip.
#
# Usage:
#   tools/check-image.sh <image>
set -euo pipefail

image="${1:?usage: tools/check-image.sh <image>}"

docker run --rm --network none --entrypoint sh "$image" -c '
  set -eu
  # find exits non-zero on unreadable directories as a non-root user.
  bundles="$(find / -xdev -path "*/pip/_vendor/certifi/cacert.pem" 2>/dev/null || true)"
  if [ -n "$bundles" ]; then
    echo "image carries a pip-vendored CA bundle: $bundles" >&2
    exit 1
  fi
  if python -c "import pip" 2>/dev/null; then
    echo "pip is still installed in the image" >&2
    exit 1
  fi
  if ls /etc/ssl/certs | grep -qi "emSign_ECC_Root_CA_-_G3"; then
    echo "system CA store still trusts emSign ECC Root CA - G3" >&2
    exit 1
  fi
  python -c "
import subprocess, sys, time, urllib.request

import httpx

# Ask httpx rather than grepping certifi, so this checks what the API clients trust.
roots = httpx.create_ssl_context().get_ca_certs()
names = [dict(rdn[0] for rdn in cert[\"subject\"]).get(\"commonName\") for cert in roots]
if not names:
    sys.exit(\"httpx loaded an empty trust store\")
if \"emSign ECC Root CA - G3\" in names:
    sys.exit(\"httpx trust store still trusts emSign ECC Root CA - G3\")

proc = subprocess.Popen([sys.executable, \"run.py\"])
try:
    for _ in range(60):
        try:
            with urllib.request.urlopen(\"http://127.0.0.1:8000/api/auth/status\", timeout=1) as r:
                assert r.status == 200
                break
        except OSError:
            time.sleep(0.5)
    else:
        sys.exit(\"server did not answer within 30s\")
finally:
    proc.terminate()
"
'

echo "image OK: $image"
