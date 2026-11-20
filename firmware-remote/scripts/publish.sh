#!/bin/sh
# Publish a built image to the hub host's firmware directory, by hand:
#   firmware-remote/scripts/publish.sh [env] [host]     (default: bridge bakenetus)
# The image goes to /var/lib/rook05/data/firmware/rook-remote-<board>-<version>.bin,
# owned by rook05. Prints the build's moniker, size and SHA-256. It does not
# tell any remote to update (remote.update does that).
set -eu
ENV=${1:-bridge}
HOST=${2:-bakenetus}
HERE=$(cd "$(dirname "$0")/.." && pwd)
case $ENV in
    bridge) BOARD=bridge-c3 ;;
    remote) BOARD=s3 ;;
    gamepad) BOARD=gamepad-s3 ;;
    *) echo "unknown env $ENV" >&2; exit 2 ;;
esac
VERSION=$(sed -n 's/^#define FW_VERSION "\(.*\)"/\1/p' "$HERE/src/main.cpp")
BIN=$HERE/.pio/build/$ENV/firmware.bin
NAME=rook-remote-$BOARD-$VERSION.bin
[ -f "$BIN" ] || { echo "no $BIN: build it first (pio run -e $ENV)" >&2; exit 1; }
MONIKER=$(head -n1 "$HERE/.pio/build/$ENV/moniker.txt" 2>/dev/null || echo unknown)
SHA=$(sha256sum "$BIN" | cut -d' ' -f1)
echo "publishing $ENV $VERSION ($MONIKER) as $NAME: $(stat -c %s "$BIN") bytes, sha256 $SHA"
scp -q "$BIN" "$HOST:/tmp/$NAME"
ssh "$HOST" "sudo -n install -o rook05 -g rook05 -m 644 /tmp/$NAME /var/lib/rook05/data/firmware/$NAME && rm -f /tmp/$NAME && sudo -n sha256sum /var/lib/rook05/data/firmware/$NAME"
echo "published $NAME, build $MONIKER"
