#!/bin/sh
# Export one Goobi process template as re-importable SQL.
#
#   integration/goobi/export_template.sh OCR_Processing > OCR_Processing.sql
#
# Goobi has no template export: the only actions on the template page are
# "new" and "edit", and the REST API creates processes *from* templates, never
# templates themselves. Templates live in MariaDB, so SQL is the only way to
# carry a workflow definition to another host (or to version it).
#
# What comes across: the template row, its steps, and each step's user-group
# assignments. Nothing else is needed, because everything a template
# references -- institution, project, ruleset, docket, user groups -- is part
# of the stock seed data and therefore already present with identical IDs on
# any fresh Goobi install. Verify that assumption on the target before
# importing (see check_ids below); it holds for an unmodified install.
#
# Deliberately NOT exported: users, processes, history, process logs. Those
# are host-specific, and user accounts in particular should be created on the
# target from its own directory rather than migrated.
set -eu

TEMPLATE="${1:?usage: $0 <template title>}"
CONTEXT="${GOOBI_CONTEXT_XML:-/etc/tomcat10/Catalina/localhost/goobi.xml}"
DB="${GOOBI_DB:-goobi}"

# Read the live credential from Tomcat's context rather than keeping a second
# copy: the install's credentials file drifts once anyone rotates the password.
DB_USER="${GOOBI_DB_USER:-$(sed -n 's/.*username="\([^"]*\)".*/\1/p' "$CONTEXT" | head -1)}"
DB_PASS="${GOOBI_DB_PASS:-$(sed -n 's/.*password="\([^"]*\)".*/\1/p' "$CONTEXT" | head -1)}"
[ -n "$DB_USER" ] && [ -n "$DB_PASS" ] || {
    echo "$0: no DB credentials; set GOOBI_DB_USER/GOOBI_DB_PASS or make $CONTEXT readable" >&2
    exit 1
}

q() { mysql -u "$DB_USER" -p"$DB_PASS" -N -B "$DB" -e "$1"; }

PID=$(q "select ProzesseID from prozesse where titel='$TEMPLATE' and IstTemplate=1;")
[ -n "$PID" ] || { echo "$0: no template titled '$TEMPLATE'" >&2; exit 1; }

# Resolve step IDs up front and inline them. A subquery inside mysqldump's
# --where fails with "Table 'schritte' was not locked with LOCK TABLES",
# because mysqldump locks only the table it is dumping.
STEPS=$(q "select group_concat(SchritteID) from schritte where ProzesseID=$PID;")
[ -n "$STEPS" ] && [ "$STEPS" != "NULL" ] || { echo "$0: template '$TEMPLATE' has no steps" >&2; exit 1; }

dump() {
    mysqldump -u "$DB_USER" -p"$DB_PASS" --no-create-info --complete-insert \
        --skip-extended-insert --no-tablespaces --compact --skip-comments \
        "$DB" "$@"
}

# The schema declares no foreign keys, so an import "succeeds" even when the
# rows it references are absent -- yielding a template that looks fine and
# then fails at process creation with "No user associated for task X". Emit
# the specific dependency checks rather than a generic warning.
DEPS=$(q "select concat_ws(' ', ProjekteID, MetadatenKonfigurationID, ifnull(docketID,0)) from prozesse where ProzesseID=$PID;")
PROJ=$(echo "$DEPS" | cut -d' ' -f1)
RULE=$(echo "$DEPS" | cut -d' ' -f2)
DOCK=$(echo "$DEPS" | cut -d' ' -f3)
GROUPS=$(q "select group_concat(distinct BenutzerGruppenID) from schritteberechtigtegruppen where schritteID in ($STEPS);")

echo "-- Goobi process template '$TEMPLATE' (ProzesseID $PID, steps $STEPS)"
echo "-- Exported $(date -u '+%Y-%m-%dT%H:%M:%SZ') from $(hostname)"
echo "-- Import:  mysql goobi < this-file.sql   (then restart Tomcat)"
echo "--"
echo "-- PRE-FLIGHT on the target. The schema has no foreign keys, so a missing"
echo "-- dependency imports cleanly and only fails later at process creation."
echo "-- Every query below must return 0."
echo "--   -- IDs must be free:"
echo "--   select count(*) from prozesse where ProzesseID=$PID;"
echo "--   select count(*) from schritte where SchritteID in ($STEPS);"
echo "--   -- dependencies must already exist:"
echo "--   select 1-count(*) from projekte where ProjekteID=$PROJ;"
echo "--   select 1-count(*) from metadatenkonfigurationen where MetadatenKonfigurationID=$RULE;"
echo "--   select 1-count(*) from dockets where docketID=$DOCK;"
echo "--   select $(echo "$GROUPS" | tr ',' '\n' | wc -l)-count(*) from benutzergruppen where BenutzergruppenID in ($GROUPS);"
echo ""
dump --where="ProzesseID=$PID" prozesse
dump --where="ProzesseID=$PID" schritte
dump --where="schritteID in ($STEPS)" schritteberechtigtegruppen
