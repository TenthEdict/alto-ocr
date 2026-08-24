#!/usr/bin/env bash
#
# Goobi workflow -> ALTO-OCR bridge.
#
# Registered in /opt/digiverso/goobi/config/goobi_scripts.xml as:
#
#   <command>/opt/digiverso/goobi/scripts/run_alto_ocr.sh {processid} "{processtitle}" {tifpath} {ocrbasispath}</command>
#
# Goobi runs this via ProcessBuilder (no shell) with each token as one argv
# entry, and marks the step FAILED if the script writes *anything* to stderr
# -- even on exit 0 (ShellScript.callShell). So everything except one summary
# line is redirected into the per-process log.
#
# Layout produced under {ocrbasispath}:
#
#   <title>_alto/            real dir -- Goobi's canonical ALTO folder
#   <title>_txt/             real dir -- Goobi's canonical plain-text folder
#   alto_ocr/
#     <title>/ALTO -> ../../<title>_alto
#     <title>/Text -> ../../<title>_txt
#     <title>/<title>.mets.xml    LibNova-style per-object METS
#
# alto_ocr/ is the view that the ALTO-OCR tool's own commands understand
# (`triage`, `repackage`, the review GUI all expect <root>/<RefID>/ALTO).
# Because ALTO/ and Text/ are symlinks onto Goobi's real folders, both
# toolchains read and write the same bytes -- no copies, and the pipeline's
# review-provenance guard sees human corrections that were made in Goobi.
#
# The process title is passed as --ref-id, so the Ref ID is the object's
# identity rather than the image folder's name ("<title>_media"). That lands
# the outputs in alto_ocr/<title>/ and stamps <title> as the METS
# OBJID/LABEL. Goobi keeps its own METS in meta.xml; this one is the LibNova
# per-object METS. To suppress it, set ALTO_OCR_EXTRA_ARGS="--no-mets".
#
set -euo pipefail

PROCESS_ID="${1:-}"
PROCESS_TITLE="${2:-}"
TIFF_DIR="${3:-}"
OCR_BASE="${4:-}"

# ---- configuration ---------------------------------------------------------
APP_DIR="${ALTO_OCR_APP_DIR:-/home/schipp0/alto-ocr}"
VENV_PYTHON="${APP_DIR}/venv/bin/python"
ENGINE="${ALTO_OCR_ENGINE:-tesseract}"
OCR_LANG="${ALTO_OCR_LANG:-eng}"
LOG_DIR="${ALTO_OCR_LOG_DIR:-/opt/digiverso/logs}"
# Extra flags, e.g. ALTO_OCR_EXTRA_ARGS="--psm 4 --binarize"
read -r -a EXTRA_ARGS <<< "${ALTO_OCR_EXTRA_ARGS:-}"

log_ts() { date '+%Y-%m-%d %H:%M:%S'; }

# ---- argument validation (before any redirection, so usage is visible) -----
if [[ -z "${PROCESS_ID}" || -z "${PROCESS_TITLE}" || -z "${TIFF_DIR}" || -z "${OCR_BASE}" ]]; then
    echo "ERROR: usage: $0 <processid> <processtitle> <tiff_dir> <ocr_base_dir>"
    exit 1
fi

mkdir -p "${LOG_DIR}"
LOG_FILE="${LOG_DIR}/alto_ocr_${PROCESS_ID}.log"

# fd 3 = the real stdout, which Goobi captures into the process journal.
# Everything else -- including stderr -- goes to the log file only.
exec 3>&1
exec >>"${LOG_FILE}" 2>&1

# One-liner back to Goobi's journal, and a matching line in the log.
say() { echo "$*" >&3; echo "[$(log_ts)] $*"; }
# Failure path: report to Goobi's journal on stdout (NOT stderr -- Goobi
# already has the non-zero exit code; stderr would only duplicate the error)
# and point at the log.
die() { say "ALTO-OCR FAILED for ${PROCESS_TITLE} (process ${PROCESS_ID}): $*  See ${LOG_FILE}"; exit 1; }

echo "[$(log_ts)] =========================================================="
echo "[$(log_ts)] process id    : ${PROCESS_ID}"
echo "[$(log_ts)] process title : ${PROCESS_TITLE}"
echo "[$(log_ts)] input images  : ${TIFF_DIR}"
echo "[$(log_ts)] ocr base      : ${OCR_BASE}"
echo "[$(log_ts)] engine        : ${ENGINE} (lang ${OCR_LANG})"

# Unreplaced Goobi variables are the classic misconfiguration: a token that
# still looks like {tifffolder} means the name is not one Goobi knows.
for arg in "${TIFF_DIR}" "${OCR_BASE}"; do
    case "${arg}" in
        \{*\}|\$\(*\)) die "unreplaced Goobi variable '${arg}' -- check the <command> in goobi_scripts.xml" ;;
    esac
done

[[ -d "${TIFF_DIR}" ]] || die "input image folder does not exist: ${TIFF_DIR}"

# ---- build the output layout ----------------------------------------------
ALTO_DIR="${OCR_BASE%/}/${PROCESS_TITLE}_alto"
TXT_DIR="${OCR_BASE%/}/${PROCESS_TITLE}_txt"
VIEW_ROOT="${OCR_BASE%/}/alto_ocr"
VIEW_DIR="${VIEW_ROOT}/${PROCESS_TITLE}"

mkdir -p "${ALTO_DIR}" "${TXT_DIR}" "${VIEW_DIR}"
ln -sfn "../../${PROCESS_TITLE}_alto" "${VIEW_DIR}/ALTO"
ln -sfn "../../${PROCESS_TITLE}_txt" "${VIEW_DIR}/Text"

SUMMARY_JSON="${LOG_DIR}/alto_summary_${PROCESS_ID}.json"
export PYTHONPATH="${APP_DIR}/src${PYTHONPATH:+:${PYTHONPATH}}"

[[ -x "${VENV_PYTHON}" ]] || die "python interpreter not executable: ${VENV_PYTHON}"

# ---- run the OCR ----------------------------------------------------------
echo "[$(log_ts)] running alto_ocr process ..."
set +e
"${VENV_PYTHON}" -m alto_ocr.main process \
    "${TIFF_DIR}" \
    "${VIEW_ROOT}" \
    --ref-id "${PROCESS_TITLE}" \
    --engine "${ENGINE}" \
    --ocr-lang "${OCR_LANG}" \
    --summary-json "${SUMMARY_JSON}" \
    ${EXTRA_ARGS[@]+"${EXTRA_ARGS[@]}"}
EXIT_CODE=$?
set -e

if [[ ${EXIT_CODE} -ne 0 ]]; then
    die "alto_ocr exited ${EXIT_CODE}"
fi

N_ALTO=$(find "${ALTO_DIR}" -maxdepth 1 -name '*.xml' | wc -l)
N_TXT=$(find "${TXT_DIR}" -maxdepth 1 -name '*.txt' | wc -l)
if [[ "${N_ALTO}" -eq 0 ]]; then
    die "alto_ocr reported success but produced no ALTO in ${ALTO_DIR}"
fi

say "ALTO-OCR done for ${PROCESS_TITLE}: ${N_ALTO} ALTO / ${N_TXT} text pages. Log: ${LOG_FILE}"
exit 0
