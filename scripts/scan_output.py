"""Scan an OCR output tree for quality anomalies — a QA triage tool.

Walks ``<output_root>/<RefID>/Text/*.txt`` (and the sibling ``ALTO/*.xml``)
and reports:

  * counts + ``summary.json`` (per-Ref-ID failures, ALTO/Text alignment)
  * **LEAKS (regression canary, expect 0)** — lines that match the engine's
    OWN stripper yet are still present in the output. On fresh output this is
    0 by construction (the engine removed them before writing); a non-zero
    count means the output is stale (produced before the current stripper) or
    the strip regressed. This is the scriptable signal: exit code 1 iff > 0.
  * **REVIEW candidates** — lines flagged by a deliberately BROAD, engine-
    INDEPENDENT model-voice heuristic (a superset of the stripper). This is
    what surfaces genuinely NEW commentary phrasings the engine doesn't yet
    know — but it also flags real captions ("The photograph shows…"), so it's
    a human-review list (use ``--show``), not a pass/fail signal. Bring any
    real leaked commentary to Claude — extending the stripper is a ~2-min edit.
  * **Known limitations** — accepted 2026-06 (expected on a minority of pages):
      - duplicate-heavy pages (glm-ocr column-doubling on dense multi-column scans)
      - CJK / non-Latin hallucination (mangled math pages)
      - empty (blank) pages

Exit code is 1 only when the regression canary fires; REVIEW candidates and
known limitations never affect it.

Usage::

    python scripts/scan_output.py OUTPUT_ROOT
    python scripts/scan_output.py OUTPUT_ROOT --show 5      # print sample snippets
    python scripts/scan_output.py OUTPUT_ROOT --csv report.csv   # per-page spreadsheet

Run from the repo root so ``src.alto_ocr`` resolves.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

# Allow running as `python scripts/scan_output.py` from the repo root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Imported ONLY as a regression canary: lines matching the engine's own
# stripper that are STILL present mean the strip wasn't applied (stale output
# or a bug) — on current-engine output this is 0 by construction. New/leaked
# commentary is surfaced by the INDEPENDENT, broader _COMMENTARY_HINTS below,
# NOT by this regex (which would be tautological for that purpose).
from src.alto_ocr._util import force_utf8_output  # noqa: E402
from src.alto_ocr.glmocr import _CHAT_COMMENTARY_RE  # noqa: E402

# Robust UTF-8 stdout/stderr (errors='replace' + Windows code page), shared
# with the CLI — OCR text (½, —, CJK) would otherwise crash prints on cp1252.
force_utf8_output()

# This script is intentionally engine-INDEPENDENT (see _COMMENTARY_HINTS): these
# thresholds are its OWN duplicate-heavy heuristic for triage, NOT a mirror of
# any engine constant, so they don't drift when engine tuning changes.
_DUP_RATIO = 0.6   # unique/total non-blank lines below this (multi-line page) = duplicate-heavy
_DUP_MIN_LINES = 8


def _nonblank(text: str) -> list[str]:
    return [s for s in (ln.strip() for ln in text.split("\n")) if s]


def _unique_ratio(nb: list[str]) -> float:
    return len(set(nb)) / len(nb) if nb else 1.0


def _is_cjk(ch: str) -> bool:
    o = ord(ch)
    return (0x4E00 <= o <= 0x9FFF or 0x3400 <= o <= 0x4DBF      # CJK ideographs (+ ext A)
            or 0x3040 <= o <= 0x30FF or 0xAC00 <= o <= 0xD7AF)  # kana, hangul


def _has_cjk_hallucination(text: str) -> bool:
    """True if any line is majority CJK — glm-ocr's degenerate-math signature."""
    for ln in text.split("\n"):
        chars = [c for c in ln if not c.isspace()]
        if len(chars) >= 3 and sum(_is_cjk(c) for c in chars) / len(chars) > 0.5:
            return True
    return False


# INDEPENDENT, deliberately-broad model-voice openers — a SUPERSET of the
# engine's stripper, so this surfaces commentary the engine did NOT remove
# (genuinely new phrasings + the colliding openers the engine intentionally
# leaves, e.g. "the photograph shows"). These are review CANDIDATES, not
# confirmed leaks: a line may be real caption text ("The photograph shows the
# 1923 class.") or leaked model commentary — a human (or Claude) judges. Kept
# leaning toward self-referential / self-talk phrasings to limit noise.
_COMMENTARY_HINTS = (
    "the image ", "this image ", "the photograph ", "the photo ", "the picture ",
    "the illustration ", "the drawing ", "the graphic ", "the scene ",
    "the text in the ", "the text provided", "the text appears", "the text reads",
    "the text is a description", "the text on the ",
    "the document is about", "the document describes", "the document discusses",
    "appears to be a", "this is a photo", "this is an image", "this is an illustration",
    "in the foreground", "in the background",
    "i'm unable", "i am unable", "i'll output", "i'll just", "i'll transcribe",
    "i will output", "let me re-read", "let me reread", "let me re-examine",
    "here is the text", "here's the text", "here is the transcription",
    "unfortunately, i", "i cannot transcribe", "i can't transcribe",
)


def _candidate_commentary(text: str) -> list[str]:
    """Lines whose lowercased start matches a broad model-voice hint (review).

    Hints that end in a space (e.g. "the image ") get a word boundary for free,
    so a plain startswith is both correct and simple.
    """
    out = []
    for ln in text.split("\n"):
        low = ln.strip().lower()
        if low and any(low.startswith(h) for h in _COMMENTARY_HINTS):
            out.append(ln.strip())
    return out


def scan_page(path: Path) -> dict:
    text = path.read_text(encoding="utf-8", errors="replace")
    nb = _nonblank(text)
    # Regression canary: lines the CURRENT engine stripper matches but are still
    # present -> the strip wasn't applied (stale output / bug). 0 on fresh output.
    leaked = [ln.strip() for ln in text.split("\n") if _CHAT_COMMENTARY_RE.match(ln)]
    return {
        "path": path,
        "ref_id": path.parent.parent.name,
        "bytes": len(text),
        "lines": len(nb),
        "uniq": _unique_ratio(nb),
        "text": text,
        "flags": {
            "leaked": leaked,                          # LEAK / exit-1 signal (expect 0)
            "candidate": _candidate_commentary(text),  # review candidates (independent, broad)
            # Known limitations / info (never affect exit code)
            "dup": len(nb) >= _DUP_MIN_LINES and _unique_ratio(nb) < _DUP_RATIO,
            "cjk": _has_cjk_hallucination(text),
            "empty": not text.strip(),
        },
    }


_LEAK_CATS = (
    ("leaked", "lines the current stripper would remove but are STILL present "
               "(stale output / regression — expect 0 on fresh output)"),
)
_REVIEW_CATS = (
    ("candidate", "model-voice lines to eyeball (real caption vs leaked commentary; "
                  "broad heuristic, independent of the engine — bring real leaks to Claude)"),
)
_INFO_CATS = (
    ("dup", "duplicate-heavy page (accepted: column-doubling on dense multi-column)"),
    ("cjk", "CJK/non-Latin hallucination (accepted: mangled math page)"),
    ("empty", "empty (blank) page"),
)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="scan_output", description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("output_root", type=Path, help="OCR output root (<root>/<RefID>/Text/*.txt)")
    ap.add_argument("--show", type=int, default=0, metavar="N",
                    help="print up to N sample pages per flagged category")
    ap.add_argument("--csv", type=Path, default=None, help="write a per-page CSV report here")
    args = ap.parse_args(argv)

    root: Path = args.output_root
    if not root.is_dir():
        print(f"error: not a directory: {root}", file=sys.stderr)
        return 2

    txts = sorted(root.glob("*/Text/*.txt"))
    if not txts:
        print(f"error: no <RefID>/Text/*.txt found under {root}", file=sys.stderr)
        return 2
    altos = sorted(root.glob("*/ALTO/*.xml"))
    ref_ids = sorted({p.parent.parent.name for p in txts})

    print(f"Scanned {root}")
    print(f"  Ref IDs: {len(ref_ids)}   Text files: {len(txts)}   ALTO files: {len(altos)}")

    sj = root / "summary.json"
    if sj.exists():
        try:
            s = json.loads(sj.read_text(encoding="utf-8"))
            print(f"  summary.json: ok={s.get('ok_count')}/{s.get('ref_id_count')}  "
                  f"failed_pages={s.get('failed_page_total')}")
            for r in s.get("ref_ids", []):
                if not r.get("ok"):
                    # Prefer failed_page_count (counts all unproduced pages of an
                    # aborted Ref ID); fall back to the detail-list length for
                    # summaries written before that field existed.
                    nfailed = r.get("failed_page_count", len(r.get("failed_pages", [])))
                    print(f"    [!] NOT OK: {r['ref_id']} ({nfailed} failed page(s))")
        except (json.JSONDecodeError, KeyError, TypeError) as e:
            print(f"  summary.json present but unreadable: {e}")

    # ALTO/Text count alignment per Ref ID (stems should match 1:1)
    mismatches = []
    for rid in ref_ids:
        na = len(list((root / rid / "ALTO").glob("*.xml")))
        nt = len(list((root / rid / "Text").glob("*.txt")))
        if na != nt:
            mismatches.append((rid, na, nt))
    if mismatches:
        print(f"\n  [!] ALTO/Text COUNT MISMATCH in {len(mismatches)} Ref ID(s):")
        for rid, na, nt in mismatches:
            print(f"      {rid}: ALTO={na} Text={nt}")

    pages = [scan_page(p) for p in txts]

    def members(cat: str) -> list[dict]:
        return [pg for pg in pages if pg["flags"][cat]]

    def sample(pg: dict) -> str:
        return f"{pg['ref_id'][-14:]}/{pg['path'].name}"

    def print_line_cats(cats, marker):
        for cat, desc in cats:
            m = members(cat)
            print(f"  [{len(m):4d}] {desc}")
            for pg in m[: args.show]:
                print(f"        {sample(pg)}  ({pg['bytes']}B)")
                for ln in pg["flags"][cat][:3]:
                    print(f"           {marker} {ln[:90]}")

    leak_total = sum(len(members(c)) for c, _ in _LEAK_CATS)
    review_total = sum(len(members(c)) for c, _ in _REVIEW_CATS)

    print("\n=== LEAKS (regression canary — expect 0; non-zero => exit 1) ===")
    if leak_total == 0:
        print("  none — no lines matching the current stripper remain in the output.")
    else:
        print_line_cats(_LEAK_CATS, "!")

    print(f"\n=== REVIEW candidates ({review_total}) — eyeball with --show; "
          f"a real leaked commentary line is a 2-min regex add ===")
    print_line_cats(_REVIEW_CATS, "?")

    print("\n=== Known limitations (accepted — expected on a minority) ===")
    for cat, desc in _INFO_CATS:
        m = members(cat)
        print(f"  [{len(m):4d}] {desc}")
        if cat != "empty":
            for pg in m[: args.show]:
                print(f"        {sample(pg)}  {pg['bytes']}B  {pg['lines']} lines  {int(pg['uniq'] * 100)}% uniq")

    if args.csv:
        with args.csv.open("w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["ref_id", "page", "bytes", "nonblank_lines", "unique_pct",
                        "leaked_lines", "candidate_lines", "dup", "cjk", "empty"])
            for pg in pages:
                fl = pg["flags"]
                w.writerow([pg["ref_id"], pg["path"].name, pg["bytes"], pg["lines"],
                            round(pg["uniq"] * 100), len(fl["leaked"]), len(fl["candidate"]),
                            int(fl["dup"]), int(fl["cjk"]), int(fl["empty"])])
        print(f"\nWrote per-page CSV: {args.csv}")

    print(f"\nVerdict: {leak_total} regression-canary leak(s), {review_total} review "
          f"candidate(s) across {len(txts)} page(s). "
          + ("Investigate the LEAKS above." if leak_total
             else "Canary clean; skim REVIEW candidates for any leaked commentary."))
    return 1 if leak_total else 0


if __name__ == "__main__":
    raise SystemExit(main())
