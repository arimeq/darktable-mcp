#!/usr/bin/env bash
# Cloud session bootstrap: darktable-cli of the user's exact GUI version, Python deps, sample RAW for tests.
set -euo pipefail
cd "$(dirname "$0")/.."
DT=5.6.1
SUDO=$([ "$(id -u)" = 0 ] || echo sudo)

dt_version() { darktable-cli --version 2>/dev/null || true; }  # --version exits 1 even when it works

if ! dt_version | grep -q "darktable $DT"; then
  tmp=$(mktemp -d)
  curl -fsSL -o "$tmp/dt.AppImage" \
    "https://github.com/darktable-org/darktable/releases/download/release-$DT/Darktable-$DT-x86_64.AppImage"
  chmod +x "$tmp/dt.AppImage"
  (cd "$tmp" && ./dt.AppImage --appimage-extract >/dev/null)  # containers have no FUSE to mount it
  $SUDO rm -rf /opt/darktable && $SUDO mv "$tmp/squashfs-root" /opt/darktable && rm -rf "$tmp"
  # AppRun sets up the bundled libs; it runs usr/bin/<first arg> only with APPIMAGE set, else the GUI
  printf '#!/bin/sh\nAPPIMAGE=1 exec /opt/darktable/AppRun darktable-cli "$@"\n' | $SUDO tee /usr/local/bin/darktable-cli >/dev/null
  $SUDO chmod +x /usr/local/bin/darktable-cli
fi
dt_version | head -1

# Same workflow as the user's GUI; export copies this darktablerc into its throwaway configdir.
mkdir -p ~/.config/darktable
grep -q '^plugins/darkroom/workflow=' ~/.config/darktable/darktablerc 2>/dev/null ||
  echo 'plugins/darkroom/workflow=scene-referred (sigmoid)' >> ~/.config/darktable/darktablerc

pip install -q -e '.[dev]'

# Samples from the user's cameras (CC0, raw.pixls.us)
mkdir -p tests/data && cd tests/data
while read -r sum url; do
  f=$(basename "$url"); [ -f "$f" ] || curl -fsSL -o "$f" "$url"
  echo "$sum  $f" | sha256sum -c -
done <<'EOF'
e975f71429b939701dd618a7bac4709c1409f4bb6aa1c3607ceeaa86031c1c4a https://raw.pixls.us/data/Canon/EOS%20450D/_MG_7191.CR2
11d90cbb564ad2c660c7a8d5d81d99c2c018aa27fdec45ed6c461fa5d24f942b https://raw.pixls.us/data/Olympus/E-M10%20Mark%20III/PA080192.ORF
EOF
