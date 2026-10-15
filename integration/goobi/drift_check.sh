#!/bin/sh
# Report where a Goobi host and this checkout disagree. Changes nothing.
#
#   integration/goobi/drift_check.sh [--no-db] [--stock DIR] [-q]
#
# Checks, against what install.sh would put there:
#   - the Goobi version deployed vs GOOBI_VERSION
#   - the bridge scripts and every file in config/ (the task link compared
#     with the URL the host already uses, so only structure can drift)
#   - each key in config/goobi_config.overrides.properties
#   - the review server's unit and Apache conf, if installed
#   - plugins.txt: required plugins with no jar, jars nobody listed
#   - templates/*.sql: re-exported from the database and diffed
#
#   --no-db      skip the template check (no database access needed)
#   --stock DIR  a stock Goobi config directory -- install/config from a
#                clone of intranda/goobi-workflow at the GOOBI_VERSION tag.
#                Host config files that differ from stock, or do not exist
#                in stock, and are not in config/ are customizations nobody
#                has captured; listing them is how to harvest an old host.
#   -q           print only the problems
#
# Exit 0: no drift. 1: drift found (each item printed). 2: usage error.
# Read-only on the host: it writes only to a private temp directory. Run it
# as root or as the Goobi user; the template check needs the database
# credentials export_template.sh reads.

set -eu

# shellcheck source=lib.sh
. "$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)/lib.sh"

NO_DB=""
STOCK=""
QUIET=""
while [ $# -gt 0 ]; do
    case "$1" in
        --no-db) NO_DB=1; shift ;;
        --stock) STOCK="${2:?--stock needs a directory}"; shift 2 ;;
        -q) QUIET=1; shift ;;
        -h|--help) sed -n '2,27p' "$0"; exit 0 ;;
        *) echo "drift_check.sh: unknown argument '$1'" >&2; exit 2 ;;
    esac
done
[ -z "$STOCK" ] || [ -d "$STOCK" ] || { echo "drift_check.sh: no directory $STOCK" >&2; exit 2; }
[ -d "$GOOBI_ROOT/config" ] || { echo "drift_check.sh: $GOOBI_ROOT/config not found -- is this a Goobi host?" >&2; exit 2; }

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT INT TERM
DRIFT=0

ok()    { [ -n "$QUIET" ] || echo "  ok       $*"; }
drift() { echo "  DRIFT    $*"; DRIFT=1; }
info()  { [ -n "$QUIET" ] || echo "  note     $*"; }
section() { [ -n "$QUIET" ] || echo "$*"; }

# same EXPECTED LIVE LABEL: compare a rendered file with the host's copy.
same() {
    if [ ! -f "$2" ]; then
        drift "$3: missing ($2)"
    elif cmp -s "$1" "$2"; then
        ok "$3"
    else
        drift "$3 differs from the kit:"
        diff -u "$1" "$2" | sed -e '1,2d' -e 's/^/             /' | head -40
    fi
}

lower() { printf '%s' "$1" | tr '[:upper:]' '[:lower:]'; }

# ---- Goobi version ------------------------------------------------------------
section "Goobi version:"
pinned="$(pinned_version)"
live_v="$(goobi_live_version || true)"
if [ -z "$live_v" ]; then
    info "cannot read the deployed version (GOOBI_WEBAPP=$GOOBI_WEBAPP); kit pins $pinned"
elif [ "$live_v" = "$pinned" ]; then
    ok "$live_v (pinned in GOOBI_VERSION)"
else
    drift "Goobi $live_v deployed, kit verified against $pinned (GOOBI_VERSION)"
fi

# ---- bridge + config overlay -------------------------------------------------
section "Bridge and config overlay:"
for s in run_alto_ocr.sh import_images.sh; do
    same "$KIT_DIR/$s" "$GOOBI_ROOT/scripts/$s" "scripts/$s"
done
for f in "$CONFIG_DIR"/*; do
    n="$(basename -- "$f")"
    case "$n" in
        goobi_config.overrides.properties) continue ;;
        "$PROPS_NAME")
            live="$GOOBI_ROOT/config/$n"
            if [ -f "$live" ]; then
                url="$(props_url "$live")"
                render_props "${url:-$PLACEHOLDER_URL}" > "$TMP/$n"
                [ "$url" != "$PLACEHOLDER_URL" ] || drift "config/$n: the task link is still the placeholder URL"
            else
                cp "$f" "$TMP/$n"
            fi
            same "$TMP/$n" "$live" "config/$n" ;;
        *) same "$f" "$GOOBI_ROOT/config/$n" "config/$n" ;;
    esac
done

live_cfg="$GOOBI_ROOT/config/goobi_config.properties"
if [ -f "$live_cfg" ]; then
    override_lines > "$TMP/overrides"
    while IFS= read -r line; do
        k="${line%%=*}"
        want="${line#*=}"
        # One "v:<value>" line per occurrence, so an empty value still counts.
        awk -v k="$k" '
            $0 !~ /^[ \t]*[#!]/ && (i = index($0, "=")) {
                key = substr($0, 1, i - 1); gsub(/^[ \t]+|[ \t]+$/, "", key)
                if (key == k) print "v:" substr($0, i + 1) }' "$live_cfg" > "$TMP/hits"
        count="$(wc -l < "$TMP/hits" | tr -d ' ')"
        got="$(tail -n 1 "$TMP/hits" | sed 's/^v://')"
        if [ "$count" -eq 0 ]; then
            drift "goobi_config.properties: $k is not set (kit: $want)"
        elif [ "$count" -gt 1 ]; then
            drift "goobi_config.properties: $k is set $count times (PROVISIONING.md section 2)"
        elif [ "$got" != "$want" ]; then
            drift "goobi_config.properties: $k=$got (kit: $want)"
        else
            ok "goobi_config.properties: $k"
        fi
    done < "$TMP/overrides"
else
    drift "goobi_config.properties: missing ($live_cfg)"
fi

if [ -n "$STOCK" ]; then
    # Plugin configs are not part of stock Goobi; a plugin's own default
    # lives in its repository, which this check cannot see. plugins.txt names
    # them, so report them rather than calling them drift.
    awk '!/^[ \t]*(#|$)/ && $5 != "-" { print $5 }' "$KIT_DIR/plugins.txt" > "$TMP/plugincfgs"
    # Customizations made on the host and never captured into config/.
    for live in "$GOOBI_ROOT"/config/*; do
        [ -f "$live" ] || continue
        n="$(basename -- "$live")"
        case "$n" in *.bak|*.bak-*|*~) continue ;; esac
        [ -e "$CONFIG_DIR/$n" ] && continue
        if grep -qxF "$n" "$TMP/plugincfgs"; then
            info "config/$n: plugin config (plugins.txt); compare it with the plugin's install/ default"
            continue
        fi
        if [ "$n" = goobi_config.properties ]; then
            # Overridden keys are accounted for; anything else that moved
            # from stock is not.
            if [ -f "$STOCK/$n" ]; then
                awk '/^[ \t]*(#|!|$)/ { next } { print }' "$STOCK/$n" | sort -u > "$TMP/stock.props"
                awk '/^[ \t]*(#|!|$)/ { next } { print }' "$live" | sort -u > "$TMP/live.props"
                cut -d= -f1 "$TMP/overrides" | sed 's/[ \t]*$//' > "$TMP/okeys"
                comm -13 "$TMP/stock.props" "$TMP/live.props" | while IFS= read -r l; do
                    key="$(printf '%s' "${l%%=*}" | sed 's/^[ \t]*//; s/[ \t]*$//')"
                    grep -qxF "$key" "$TMP/okeys" && continue
                    # This output gets pasted into notes and commits: never
                    # print a credential's value.
                    case "$(lower "$key")" in
                        *secret*|*password*|*passwd*|*token*) echo "$key=<redacted>" ;;
                        *) echo "$l" ;;
                    esac
                done > "$TMP/uncaptured"
                if [ -s "$TMP/uncaptured" ]; then
                    drift "goobi_config.properties: lines changed from stock and not in the overrides file:"
                    sed 's/^/             /' "$TMP/uncaptured"
                fi
            fi
        elif [ ! -f "$STOCK/$n" ]; then
            drift "config/$n: on the host, not in stock Goobi, not captured in config/"
        elif ! cmp -s "$STOCK/$n" "$live"; then
            drift "config/$n: changed from stock Goobi, not captured in config/"
        fi
    done
fi

# ---- review server -----------------------------------------------------------
section "Review server:"
unit="$SYSTEMD_DIR/$UNIT_NAME"
if [ -f "$unit" ]; then
    render_unit "$(unit_app_dir "$unit")" > "$TMP/$UNIT_NAME"
    same "$TMP/$UNIT_NAME" "$unit" "$UNIT_NAME"
else
    info "$UNIT_NAME not installed (install.sh --review)"
fi
apache="$APACHE_CONF_DIR/$APACHE_NAME"
if [ -f "$apache" ]; then
    # Filling in the Auth lines is the expected local change, so compare
    # with every Auth*/Require line set aside on both sides.
    strip_auth() { grep -Ev '^[[:space:]]*#?[[:space:]]*(Auth[A-Za-z]*|Require)[[:space:]]' "$1" || true; }
    strip_auth "$KIT_DIR/$APACHE_NAME" > "$TMP/apache.kit"
    strip_auth "$apache" > "$TMP/apache.live"
    same "$TMP/apache.kit" "$TMP/apache.live" "$APACHE_NAME (Auth lines aside)"
    if ! grep -Eq '^[[:space:]]*Require[[:space:]]' "$apache"; then
        drift "$APACHE_NAME has no active Require line: /ocr-review/ is open to anyone who reaches Apache"
    fi
else
    info "$APACHE_NAME not installed (install.sh --review)"
fi

# ---- plugins -----------------------------------------------------------------
section "Plugins:"
find "$GOOBI_ROOT/plugins" -name '*.jar' -type f 2>/dev/null \
    | sed 's|.*/||' | sort > "$TMP/jars"
: > "$TMP/claimed"
while read -r status repo glob title cfg; do
    case "$status" in ''|\#*) continue ;; esac
    g="$(lower "$glob")"
    hits=""
    while IFS= read -r j; do
        # shellcheck disable=SC2254
        case "$(lower "$j")" in $g) hits="$hits $j"; echo "$j" >> "$TMP/claimed" ;; esac
    done < "$TMP/jars"
    if [ -n "$hits" ]; then
        ok "$repo:$hits"
    elif [ "$status" = required ]; then
        drift "$repo: no jar matching '$glob' under $GOOBI_ROOT/plugins (task plugin $title)"
    else
        info "$repo: not installed ($status)"
    fi
    if [ "$cfg" != "-" ] && [ -n "$hits" ] && [ ! -f "$GOOBI_ROOT/config/$cfg" ]; then
        drift "$repo: config $cfg missing from $GOOBI_ROOT/config"
    fi
done < "$KIT_DIR/plugins.txt"
sort -u "$TMP/claimed" > "$TMP/claimed.sorted"
comm -23 "$TMP/jars" "$TMP/claimed.sorted" > "$TMP/unlisted"
if [ -s "$TMP/unlisted" ]; then
    drift "jars installed but not in plugins.txt:"
    sed 's/^/             /' "$TMP/unlisted"
fi

# ---- templates ---------------------------------------------------------------
section "Templates:"
found=""
for sql in "$KIT_DIR"/templates/*.sql; do
    [ -f "$sql" ] || continue
    found=1
    title="$(basename -- "$sql" .sql)"
    if [ -n "$NO_DB" ]; then
        info "$title: skipped (--no-db)"
        continue
    fi
    if ! "$KIT_DIR/export_template.sh" "$title" > "$TMP/$title.sql" 2> "$TMP/$title.err"; then
        drift "$title: cannot export from the database: $(head -1 "$TMP/$title.err")"
        continue
    fi
    # The header's export time and host change on every run.
    grep -v '^-- Exported ' "$sql" > "$TMP/$title.kit" || true
    grep -v '^-- Exported ' "$TMP/$title.sql" > "$TMP/$title.live" || true
    same "$TMP/$title.kit" "$TMP/$title.live" "template $title"
done
[ -n "$found" ] || info "no templates/*.sql committed yet (see templates/README.md)"

echo
if [ "$DRIFT" -eq 0 ]; then
    echo "No drift."
else
    echo "Drift found: commit the host's change here, or re-run install.sh to put the kit's back."
fi
exit "$DRIFT"
