#!/usr/bin/env bash
# Cloud session bootstrap: darktable-cli of the user's exact GUI version, Python deps, sample RAW for tests.
set -euo pipefail
cd "$(dirname "$0")/.."
DT=5.6.1
SUDO=$([ "$(id -u)" = 0 ] || echo sudo)

if ! darktable-cli --version 2>/dev/null | grep -q "darktable $DT"; then
  tmp=$(mktemp -d)
  curl -fsSL -o "$tmp/dt.AppImage" \
    "https://github.com/darktable-org/darktable/releases/download/release-$DT/Darktable-$DT-x86_64.AppImage"
  chmod +x "$tmp/dt.AppImage"
  (cd "$tmp" && ./dt.AppImage --appimage-extract >/dev/null)  # containers have no FUSE to mount it
  $SUDO rm -rf /opt/darktable && $SUDO mv "$tmp/squashfs-root" /opt/darktable && rm -rf "$tmp"
  # AppRun sets up the bundled libs and runs usr/bin/<first arg>
  printf '#!/bin/sh\nexec /opt/darktable/AppRun darktable-cli "$@"\n' | $SUDO tee /usr/local/bin/darktable-cli >/dev/null
  $SUDO chmod +x /usr/local/bin/darktable-cli
fi
darktable-cli --version | head -1

# Same workflow as the user's GUI; export copies this darktablerc into its throwaway configdir.
mkdir -p ~/.config/darktable
grep -q '^plugins/darkroom/workflow=' ~/.config/darktable/darktablerc 2>/dev/null ||
  echo 'plugins/darkroom/workflow=scene-referred (sigmoid)' >> ~/.config/darktable/darktablerc

pip install -q -e '.[dev]'

# Canon EOS 450D sample (CC0, raw.pixls.us), the user's camera
mkdir -p tests/data
[ -f tests/data/_MG_7191.CR2 ] ||
  curl -fsSL -o tests/data/_MG_7191.CR2 "https://raw.pixls.us/data/Canon/EOS%20450D/_MG_7191.CR2"
echo "e975f71429b939701dd618a7bac4709c1409f4bb6aa1c3607ceeaa86031c1c4a  tests/data/_MG_7191.CR2" | sha256sum -c -
