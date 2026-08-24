#!/usr/bin/env bash
#
# Goobi workflow -> post-processed image import.
#
# Registered in /opt/digiverso/goobi/config/goobi_scripts.xml as:
#
#   <command>/opt/digiverso/goobi/scripts/import_images.sh {processid} "{processtitle}" {origpath}</command>
#
# Copies one object's post-processed images from the digitization share into
# the process's master folder, so QC2 and OCR have something to work on. The
# source folder is found by process title: the title IS the Ref ID, and the
# share stages each object under <import root>/<RefID>/.
#
# Same three constraints as run_alto_ocr.sh, for the same reasons: Goobi marks
# a step FAILED if the script writes anything to stderr (even on exit 0), the
# args arrive as a ProcessBuilder argv list with no shell, and only one
# summary line should reach the process journal.
#
# COPIES, never moves. The share's own workflow keeps images through its
# ReviewToRemove stage; deleting them here would destroy the only copy before
# anyone has reviewed the results.
#
# Safe to re-fire: rsync transfers only what is missing or differs, so a
# re-run after a partial copy completes it instead of starting over.
#
set -euo pipefail

PROCESS_ID="${1:-}"
PROCESS_TITLE="${2:-}"
DEST_DIR="${3:-}"

# ---- configuration ---------------------------------------------------------
IMPORT_ROOT="${GOOBI_IMPORT_ROOT:-/mnt/y/_CurrentProjects/04_ImportImages}"
LOG_DIR="${GOOBI_IMPORT_LOG_DIR:-/opt/digiverso/logs}"
# Page formats to bring across, top level only. Order matters: subdirectories
# are pruned first, then page files admitted, then everything else refused --
# so Capture One sessions, sidecars and Thumbs.db all stay on the share.
# Goobi lists the master folder flat, so a nested tree must not be copied in
# as-is; nested pages are detected below and refused rather than mangled.
FILTERS=(--exclude='*/' --include='*.tif' --include='*.TIF' --include='*.tiff'
         --include='*.TIFF' --include='*.pdf' --include='*.PDF' --exclude='*')

log_ts() { date '+%Y-%m-%d %H:%M:%S'; }

if [[ -z "${PROCESS_ID}" || -z "${PROCESS_TITLE}" || -z "${DEST_DIR}" ]]; then
    echo "ERROR: usage: $0 <processid> <processtitle> <destination_dir>"
    exit 1
fi

mkdir -p "${LOG_DIR}"
LOG_FILE="${LOG_DIR}/import_images_${PROCESS_ID}.log"
# Shared between Goobi (tomcat) and operators re-running by hand; whoever
# creates it must not lock the other out.
if [[ ! -e "${LOG_FILE}" ]]; then
    : > "${LOG_FILE}" 2>/dev/null || true
fi
chmod g+w "${LOG_FILE}" 2>/dev/null || true

# fd 3 = the real stdout Goobi captures; everything else goes to the log.
exec 3>&1
exec >>"${LOG_FILE}" 2>&1

say() { echo "$*" >&3; echo "[$(log_ts)] $*"; }
die() { say "IMAGE IMPORT FAILED for ${PROCESS_TITLE} (process ${PROCESS_ID}): $*  See ${LOG_FILE}"; exit 1; }

SRC_DIR="${IMPORT_ROOT%/}/${PROCESS_TITLE}"

echo "[$(log_ts)] =========================================================="
echo "[$(log_ts)] process id    : ${PROCESS_ID}"
echo "[$(log_ts)] process title : ${PROCESS_TITLE}"
echo "[$(log_ts)] source        : ${SRC_DIR}"
echo "[$(log_ts)] destination   : ${DEST_DIR}"

for arg in "${DEST_DIR}"; do
    case "${arg}" in
        \{*\}|\$\(*\)) die "unreplaced Goobi variable '${arg}' -- check the <command> in goobi_scripts.xml" ;;
    esac
done

# An unmounted share looks exactly like an empty one to `ls`, and importing
# zero images "successfully" would send an empty object on to QC2 and OCR.
# Check the mount point itself before blaming the object.
[[ -d "${IMPORT_ROOT}" ]] || die "import root does not exist: ${IMPORT_ROOT} (is the share mounted?)"
if [[ -z "$(ls -A "${IMPORT_ROOT}" 2>/dev/null)" ]]; then
    die "import root ${IMPORT_ROOT} is empty -- the share is probably not mounted"
fi
[[ -d "${SRC_DIR}" ]] || die "no staged folder for this object: ${SRC_DIR}"

N_SRC=$(find "${SRC_DIR}" -maxdepth 1 -type f \( -iname '*.tif' -o -iname '*.tiff' -o -iname '*.pdf' \) | wc -l)
N_NESTED=$(find "${SRC_DIR}" -mindepth 2 -type f \( -iname '*.tif' -o -iname '*.tiff' -o -iname '*.pdf' \) | wc -l)

# Goobi lists the master folder flat -- a page in a subfolder would be
# invisible to QC2 and to the metadata editor. Flattening here could silently
# collide two pages that share a filename in different subfolders, so refuse
# and let a human decide.
if [[ "${N_NESTED}" -gt 0 ]]; then
    die "${N_NESTED} page file(s) sit in subfolders of ${SRC_DIR}; Goobi's master folder must be flat -- flatten the staged object first"
fi
[[ "${N_SRC}" -gt 0 ]] || die "no TIF/PDF files directly in ${SRC_DIR}"
echo "[$(log_ts)] source holds ${N_SRC} page file(s)"

mkdir -p "${DEST_DIR}" || die "cannot create destination ${DEST_DIR}"
[[ -w "${DEST_DIR}" ]] || die "destination not writable: ${DEST_DIR}"

# --checksum, not size-and-time: SMB timestamps are unreliable enough that a
# truncated earlier transfer can look complete by size+mtime alone. Costs a
# read of both sides, which is the right trade for preservation copies.
echo "[$(log_ts)] copying ..."
set +e
rsync -rt --checksum --no-perms --no-owner --no-group --stats \
    "${FILTERS[@]}" "${SRC_DIR}/" "${DEST_DIR}/"
RC=$?
set -e
[[ ${RC} -eq 0 ]] || die "rsync exited ${RC}"

N_DEST=$(find "${DEST_DIR}" -maxdepth 1 -type f \( -iname '*.tif' -o -iname '*.tiff' -o -iname '*.pdf' \) | wc -l)
if [[ "${N_DEST}" -lt "${N_SRC}" ]]; then
    die "only ${N_DEST} of ${N_SRC} page file(s) arrived in ${DEST_DIR}"
fi

say "Imported ${N_DEST} page file(s) for ${PROCESS_TITLE} from ${SRC_DIR}. Log: ${LOG_FILE}"
exit 0
