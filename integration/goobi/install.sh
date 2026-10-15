#!/bin/sh
# Provision a Goobi host with the ALTO-OCR integration, from this checkout.
#
#   sudo integration/goobi/install.sh [--app-dir DIR]               # the bridge
#   sudo integration/goobi/install.sh --config --review-url URL     # + config overlay
#   sudo integration/goobi/install.sh --review                      # + review server
#   sudo integration/goobi/install.sh --all --review-url URL        # all of it
#
#   (default)        the OCR bridge: run_alto_ocr.sh and import_images.sh into
#                    scripts/, config/goobi_scripts.xml into config/.
#   --config         also every other file in config/, the task link rendered
#                    with --review-url, and the keys in
#                    config/goobi_config.overrides.properties set in
#                    goobi_config.properties.
#   --review         also the review server: its systemd unit (enabled, NOT
#                    started) and Apache conf (installed, NOT enabled -- it
#                    authenticates nobody until you fill in its Auth lines).
#   --all            --config and --review.
#   --review-url URL the review UI as a corrector's browser reaches it, e.g.
#                    https://goobi.lib.purdue.edu/ocr-review/ (needed by --config).
#   --app-dir DIR    where the application and its venv live (default: this
#                    checkout).
#
# Idempotent. A file that already matches is left alone; one that differs is
# copied to <file>.bak-<UTC time> first. Never restarts Tomcat or Apache,
# never starts the review service, never touches the database -- each of
# those is printed as a next step instead. drift_check.sh verifies the result.

set -eu

# shellcheck source=lib.sh
. "$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)/lib.sh"

APP_DIR=""
DO_CONFIG=""
DO_REVIEW=""
REVIEW_URL=""

while [ $# -gt 0 ]; do
    case "$1" in
        --app-dir) APP_DIR="${2:?--app-dir needs a path}"; shift 2 ;;
        --review-url) REVIEW_URL="${2:?--review-url needs a URL}"; shift 2 ;;
        --config) DO_CONFIG=1; shift ;;
        --review) DO_REVIEW=1; shift ;;
        --all) DO_CONFIG=1; DO_REVIEW=1; shift ;;
        -h|--help) sed -n '2,27p' "$0"; exit 0 ;;
        *) echo "install.sh: unknown argument '$1'" >&2; exit 2 ;;
    esac
done

die() { echo "install.sh: $*" >&2; exit 1; }

# ALTO_OCR_INSTALL_ROOTLESS=1 installs into a scratch tree (GOOBI_ROOT,
# SYSTEMD_DIR, APACHE_CONF_DIR pointed elsewhere) without root or chown --
# for trying the kit out, never for a real host.
ROOTLESS="${ALTO_OCR_INSTALL_ROOTLESS:-}"
if [ -z "$ROOTLESS" ] && [ "$(id -u)" -ne 0 ]; then
    die "must run as root (writes into $GOOBI_ROOT)."
fi

[ -n "$APP_DIR" ] || APP_DIR="$REPO_DIR"
case "$APP_DIR" in /*) ;; *) die "--app-dir must be an absolute path: $APP_DIR" ;; esac

if [ -n "$DO_CONFIG" ]; then
    # The committed file carries a placeholder; installing it unrendered
    # would put a dead link on every Transcript Correction task.
    [ -n "$REVIEW_URL" ] || die "--config needs --review-url (the link on the Transcript Correction task)."
    case "$REVIEW_URL" in
        http://*/|https://*/) ;;
        *) die "--review-url must be an http(s) URL ending in '/': $REVIEW_URL" ;;
    esac
fi

for d in "$GOOBI_ROOT/scripts" "$GOOBI_ROOT/config"; do
    [ -d "$d" ] || die "$d not found -- is this a Goobi host?"
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
if [ -n "$DO_REVIEW" ] && ! "$APP_DIR/venv/bin/python" -c "import fastapi, uvicorn" 2>/dev/null; then
    # Enabling a unit whose first start would crash is worse than refusing.
    echo "install.sh: the review server's web dependencies are missing:" >&2
    echo "    $APP_DIR/venv/bin/pip install -r $APP_DIR/requirements-web.txt" >&2
    exit 1
fi

STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT INT TERM
RESTART_GOOBI=""
NEXT=""
note() { NEXT="$NEXT
  $*"; }

# put SRC DEST MODE OWNER: install SRC as DEST unless DEST already matches;
# back up a differing DEST first.
put() {
    if [ -f "$2" ] && cmp -s "$1" "$2"; then
        echo "  unchanged  $2"
        return 1
    fi
    if [ -f "$2" ]; then
        cp -p "$2" "$2.bak-$STAMP"
        echo "  backed up  $2.bak-$STAMP"
    fi
    if [ -n "$ROOTLESS" ]; then
        install -m "$3" "$1" "$2"
    else
        install -o "$4" -g "$4" -m "$3" "$1" "$2"
    fi
    echo "  installed  $2"
    return 0
}

have() { command -v "$1" >/dev/null 2>&1; }

# ---- the bridge (always) ----------------------------------------------------
echo "OCR bridge:"
for s in run_alto_ocr.sh import_images.sh; do
    put "$KIT_DIR/$s" "$GOOBI_ROOT/scripts/$s" 755 "$GOOBI_USER" || true
done
# goobi_scripts.xml is REPLACED, not merged: Goobi's sample entries
# (Tiff-Header, Copy images, Compression) are dropped on purpose. The backup
# keeps whatever a host had, for merging by hand.
put "$CONFIG_DIR/goobi_scripts.xml" "$GOOBI_ROOT/config/goobi_scripts.xml" 644 "$GOOBI_USER" \
    && RESTART_GOOBI=1

# The wrapper writes its per-process logs here; Goobi's own log dir already
# exists on a normal install, but a fresh VM may not have it yet.
LOG_DIR="$(dirname -- "$GOOBI_ROOT")/logs"
if [ ! -d "$LOG_DIR" ]; then
    if [ -n "$ROOTLESS" ]; then mkdir -p "$LOG_DIR"
    else install -d -o "$GOOBI_USER" -g "$GOOBI_USER" -m 775 "$LOG_DIR"; fi
    echo "  created    $LOG_DIR"
fi

if [ "$APP_DIR" != "$DEFAULT_APP_DIR" ]; then
    note "The application is at $APP_DIR, not the wrapper's built-in default."
    note "  Export ALTO_OCR_APP_DIR=$APP_DIR in Tomcat's environment"
    note "  (/etc/default/tomcat10), or OCR steps will not find it."
fi

# ---- config overlay ---------------------------------------------------------
if [ -n "$DO_CONFIG" ]; then
    echo "Config overlay:"
    render_props "$REVIEW_URL" > "$TMP/$PROPS_NAME"
    put "$TMP/$PROPS_NAME" "$GOOBI_ROOT/config/$PROPS_NAME" 644 "$GOOBI_USER" \
        && RESTART_GOOBI=1
    # Anything else captured into config/ (plugin configs, ...) goes in as is.
    for f in "$CONFIG_DIR"/*; do
        n="$(basename -- "$f")"
        case "$n" in
            goobi_scripts.xml|"$PROPS_NAME"|goobi_config.overrides.properties) continue ;;
        esac
        put "$f" "$GOOBI_ROOT/config/$n" 644 "$GOOBI_USER" && RESTART_GOOBI=1
    done
    live="$GOOBI_ROOT/config/goobi_config.properties"
    if [ -f "$live" ]; then
        apply_overrides "$live" > "$TMP/goobi_config.properties"
        put "$TMP/goobi_config.properties" "$live" 644 "$GOOBI_USER" && RESTART_GOOBI=1
    else
        echo "install.sh: $live not found -- overrides not applied" >&2
        exit 1
    fi
    note "Processes created before the task link existed show an empty link:"
    note "  backfill them once with GoobiScript propertySet (see the end of"
    note "  config/$PROPS_NAME)."
fi

# ---- review server ----------------------------------------------------------
if [ -n "$DO_REVIEW" ]; then
    echo "Review server:"
    [ -d "$SYSTEMD_DIR" ] || die "$SYSTEMD_DIR not found -- no systemd here?"
    [ -d "$APACHE_CONF_DIR" ] || die "$APACHE_CONF_DIR not found -- install apache2 first."
    render_unit "$APP_DIR" > "$TMP/$UNIT_NAME"
    if put "$TMP/$UNIT_NAME" "$SYSTEMD_DIR/$UNIT_NAME" 644 root; then
        if [ -z "$ROOTLESS" ] && have systemctl; then
            systemctl daemon-reload
            systemctl enable "$UNIT_NAME" >/dev/null 2>&1 && echo "  enabled    $UNIT_NAME"
        else
            note "Run: systemctl daemon-reload && systemctl enable $UNIT_NAME"
        fi
    fi
    put "$KIT_DIR/$APACHE_NAME" "$APACHE_CONF_DIR/$APACHE_NAME" 644 root || true
    if [ -z "$ROOTLESS" ] && have a2enmod; then
        a2enmod -q proxy proxy_http headers
    else
        note "Run: a2enmod proxy proxy_http headers"
    fi
    note "The review server rewrites ALTO and authenticates nobody. Fill in the"
    note "  Auth lines in $APACHE_CONF_DIR/$APACHE_NAME to match Goobi's,"
    note "  THEN: a2enconf ${APACHE_NAME%.conf} && systemctl reload apache2"
    note "Start it: systemctl start ${UNIT_NAME%.service}"
fi

# ---- what is left for the operator ------------------------------------------
pinned="$(pinned_version)"
live_v="$(goobi_live_version || true)"
if [ -n "$live_v" ] && [ "$live_v" != "$pinned" ]; then
    note "Goobi here is $live_v but the kit is verified against $pinned"
    note "  (GOOBI_VERSION). Re-verify the README's Goobi constraints first."
fi
[ -n "$RESTART_GOOBI" ] && note "Goobi reads its config at startup: systemctl restart tomcat10"
note "Plugins (plugins.txt) and templates (templates/) are not installed by"
note "  this script; drift_check.sh reports what is still missing."

echo
echo "Next steps:$NEXT"
