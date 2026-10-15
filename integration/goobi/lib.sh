# Shared by install.sh and drift_check.sh. POSIX sh; sourced, not executed.
#
# Everything that decides what a managed file should look like on a host
# lives here, so the installer and the drift check can never disagree about
# it: the check renders each file exactly as the installer would and diffs.

# ---- where things live (every one overridable from the environment) -------
GOOBI_ROOT="${GOOBI_ROOT:-/opt/digiverso/goobi}"
GOOBI_USER="${GOOBI_USER:-tomcat}"
GOOBI_WEBAPP="${GOOBI_WEBAPP:-/var/lib/tomcat10/webapps/goobi}"
SYSTEMD_DIR="${SYSTEMD_DIR:-/etc/systemd/system}"
APACHE_CONF_DIR="${APACHE_CONF_DIR:-/etc/apache2/conf-available}"

KIT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
REPO_DIR="$(CDPATH= cd -- "$KIT_DIR/../.." && pwd)"
CONFIG_DIR="$KIT_DIR/config"
OVERRIDES="$CONFIG_DIR/goobi_config.overrides.properties"
PROPS_NAME="goobi_processProperties.xml"
UNIT_NAME="alto-ocr-review.service"
APACHE_NAME="apache-ocr-review.conf"

# The values baked into the committed files, which rendering replaces with
# the host's own. Keep in step with alto-ocr-review.service and
# config/goobi_processProperties.xml.
DEFAULT_APP_DIR="/home/schipp0/alto-ocr"
PLACEHOLDER_URL="https://goobi.example.purdue.edu/ocr-review/"

# The line install.sh puts above override keys it had to append.
OVERRIDES_MARKER="# --- set by ALTO-OCR integration/goobi/install.sh (see config/goobi_config.overrides.properties) ---"

# Escape a string for the replacement side of a sed s||| command.
sed_repl() {
    printf '%s' "$1" | sed 's/[\\&|]/\\&/g'
}

# Escape a string for the pattern side (basic regex) of a sed s||| command.
sed_pat() {
    printf '%s' "$1" | sed 's/[]\\.*^$[|]/\\&/g'
}

# render_unit APP_DIR: the systemd unit as it should be installed for an
# application at APP_DIR, running as GOOBI_USER.
render_unit() {
    sed -e "s|$(sed_pat "$DEFAULT_APP_DIR")|$(sed_repl "$1")|g" \
        -e "s|^User=tomcat\$|User=$(sed_repl "$GOOBI_USER")|" \
        -e "s|^Group=tomcat\$|Group=$(sed_repl "$GOOBI_USER")|" \
        "$KIT_DIR/$UNIT_NAME"
}

# render_props URL: the process-properties file with the task link pointing
# at URL (the review UI as a corrector's browser reaches it).
render_props() {
    sed "s|$(sed_pat "$PLACEHOLDER_URL")|$(sed_repl "$1")|g" "$CONFIG_DIR/$PROPS_NAME"
}

# props_url FILE: the link URL an installed process-properties file carries.
props_url() {
    sed -n 's:.*<defaultvalue>\(.*\)</defaultvalue>.*:\1:p' "$1" | head -1
}

# unit_app_dir FILE: the application directory an installed unit runs from.
unit_app_dir() {
    sed -n 's/^WorkingDirectory=//p' "$1" | head -1
}

# override_lines: each key=value line of the overrides file, comments and
# blank lines dropped, keys trimmed.
override_lines() {
    awk '/^[ \t]*(#|$)/ { next }
         { i = index($0, "="); if (!i) next
           k = substr($0, 1, i - 1); gsub(/^[ \t]+|[ \t]+$/, "", k)
           print k "=" substr($0, i + 1) }' "$OVERRIDES"
}

# apply_overrides FILE: FILE (goobi_config.properties) with every override
# set. A key already present has its FIRST line rewritten in place and every
# later line for it dropped -- which also cures the duplicated-file problem
# (PROVISIONING.md section 2) for these keys. A key not present is appended
# under a marker line. Commented-out lines are never touched. Applying twice
# gives the same bytes as applying once, so install.sh stays idempotent.
apply_overrides() {
    override_lines | awk -v marker="$OVERRIDES_MARKER" '
        FNR == NR { i = index($0, "="); k = substr($0, 1, i - 1)
                    val[k] = substr($0, i + 1); order[++n] = k; next }
        { line = $0
          if (line == marker) hasmark = 1
          if (line !~ /^[ \t]*[#!]/ && (i = index(line, "="))) {
              k = substr(line, 1, i - 1); gsub(/^[ \t]+|[ \t]+$/, "", k)
              if (k in val) {
                  if (k in done) next
                  print k "=" val[k]; done[k] = 1; next
              }
          }
          print line }
        END { for (j = 1; j <= n; j++) {
                  k = order[j]
                  if (k in done) continue
                  if (!hasmark && !marked) { print ""; print marker; marked = 1 }
                  print k "=" val[k]
              } }' - "$1"
}

# goobi_live_version: the Goobi release deployed in GOOBI_WEBAPP, read from
# the Maven descriptor Maven packs into the WAR; empty when not found.
goobi_live_version() {
    f="$GOOBI_WEBAPP/META-INF/maven/io.goobi.workflow/workflow-core/pom.properties"
    [ -r "$f" ] && sed -n 's/^version=//p' "$f" | head -1
}

pinned_version() {
    sed -n '1s/[[:space:]]*$//p' "$KIT_DIR/GOOBI_VERSION"
}
