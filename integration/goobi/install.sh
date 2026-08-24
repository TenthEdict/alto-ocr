#!/bin/sh
# Install the Goobi workflow <-> ALTO-OCR bridge onto a Goobi host.
#
#   sudo integration/goobi/install.sh [--app-dir /path/to/alto-ocr]
#
# Idempotent: re-running overwrites the two managed files and nothing else.
# Deliberately does NOT restart Tomcat -- on a production host that is the
# operator's call, and the command is printed at the end.
#
# goobi_scripts.xml is REPLACED, not merged. Goobi ships sample entries
# (Tiff-Header, Copy images, Compression) that this file intentionally drops;
# if a host has hand-added entries, merge by hand instead of running this.

set -eu

GOOBI_ROOT="${GOOBI_ROOT:-/opt/digiverso/goobi}"
GOOBI_USER="${GOOBI_USER:-tomcat}"
APP_DIR=""

while [ $# -gt 0 ]; do
    case "$1" in
        --app-dir) APP_DIR="${2:?--app-dir needs a path}"; shift 2 ;;
        -h|--help) sed -n '2,12p' "$0"; exit 0 ;;
        *) echo "install.sh: unknown argument '$1'" >&2; exit 2 ;;
    esac
done

if [ "$(id -u)" -ne 0 ]; then
    echo "install.sh: must run as root (writes into $GOOBI_ROOT)." >&2
    exit 1
fi

SRC_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
REPO_DIR="$(CDPATH= cd -- "$SRC_DIR/../.." && pwd)"
[ -n "$APP_DIR" ] || APP_DIR="$REPO_DIR"

for d in "$GOOBI_ROOT/scripts" "$GOOBI_ROOT/config"; do
    [ -d "$d" ] || { echo "install.sh: $d not found -- is this a Goobi host?" >&2; exit 1; }
done

# The wrapper finds the application through ALTO_OCR_APP_DIR, defaulting to
# the path baked in at the top of the script. Check the venv exists at the
# path this host will actually use rather than discovering it at OCR time.
if [ ! -x "$APP_DIR/venv/bin/python" ]; then
    echo "install.sh: no venv at $APP_DIR/venv -- create it first:" >&2
    echo "    python3 -m venv $APP_DIR/venv" >&2
    echo "    $APP_DIR/venv/bin/pip install -r $APP_DIR/requirements.txt" >&2
    exit 1
fi

install -o "$GOOBI_USER" -g "$GOOBI_USER" -m 755 \
    "$SRC_DIR/run_alto_ocr.sh" "$GOOBI_ROOT/scripts/run_alto_ocr.sh"
install -o "$GOOBI_USER" -g "$GOOBI_USER" -m 644 \
    "$SRC_DIR/goobi_scripts.xml" "$GOOBI_ROOT/config/goobi_scripts.xml"

# The wrapper writes its per-process logs here; Goobi's own log dir already
# exists on a normal install, but a fresh VM may not have it yet.
LOG_DIR="$(dirname -- "$GOOBI_ROOT")/logs"
[ -d "$LOG_DIR" ] || install -d -o "$GOOBI_USER" -g "$GOOBI_USER" -m 775 "$LOG_DIR"

echo "installed:"
echo "  $GOOBI_ROOT/scripts/run_alto_ocr.sh"
echo "  $GOOBI_ROOT/config/goobi_scripts.xml"
if [ "$APP_DIR" != "/home/schipp0/alto-ocr" ]; then
    echo
    echo "NOTE: the application is at $APP_DIR, which is not the wrapper's"
    echo "      built-in default. Point the wrapper at it by exporting"
    echo "      ALTO_OCR_APP_DIR=$APP_DIR in Tomcat's environment"
    echo "      (/etc/default/tomcat10), or edit APP_DIR in the wrapper."
fi
echo
echo "goobi_scripts.xml is read at startup -- restart Goobi to pick it up:"
echo "  systemctl restart tomcat10"
