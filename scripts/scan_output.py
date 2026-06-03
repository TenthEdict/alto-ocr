"""Scan an OCR output tree for quality anomalies — a QA triage tool.

Walks ``<output_root>/<RefID>/Text/*.txt`` (and the sibling ``ALTO/*.xml``)
and reports, using the SAME helpers the engine uses so the checks stay in
sync with the shipped behavior:

  * counts + ``summary.json`` (per-Ref-ID failures, ALTO/Text alignment)
  * **LEAKS** — high-signal problems worth investigating / bringing to Claude:
      - residual chat commentary (lines the shipped stripper regex matches —
        i.e. a NEW phrasing that slipped through: a 2-minute regex addition)
      - whole-page image descriptions
      - periodic repetition loops the de-loop would still collapse
  * **Known limitations** — accepted 2026-06 (expected on a minority of pages):
      - duplicate-heavy pages (glm-ocr column-doubling on dense multi-column scans)
      - CJK / non-Latin hallucination (mangled math pages)
      - empty (blank) pages

Exit code is 1 when any LEAK is found (0 otherwise), so it is scriptable.
Known limitations never affect the exit code.

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

from src.alto_ocr.glmocr import (  # noqa: E402
    _CHAT_COMMENTARY_RE,
    _collapse_repetition_loops,
    _looks_like_image_description,
)

# Windows consoles default to cp1252; OCR text (½, —, CJK) would crash prints.
try:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
except (AttributeError, ValueError):  # pragma: no cover - non-reconfigurable stream
    pass

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


def scan_page(path: Path) -> dict:
    text = path.read_text(encoding="utf-8", errors="replace")
    nb = _nonblank(text)
    commentary = [ln.strip() for ln in text.split("\n") if _CHAT_COMMENTARY_RE.match(ln)]
    return {
        "path": path,
        "ref_id": path.parent.parent.name,
        "bytes": len(text),
        "lines": len(nb),
        "uniq": _unique_ratio(nb),
        "text": text,
        "flags": {
            # LEAKS (high signal)
            "commentary": commentary,                                            # list (truthy if any)
            "description": _looks_like_image_description(text),
            "loop": bool(text.strip()) and _collapse_repetition_loops(text) != text,
            # Known limitations / info
            "dup": len(nb) >= _DUP_MIN_LINES and _unique_ratio(nb) < _DUP_RATIO,
            "cjk": _has_cjk_hallucination(text),
            "empty": not text.strip(),
        },
    }


_LEAK_CATS = (
    ("commentary", "residual chat commentary (NEW phrasing — bring to Claude)"),
    ("description", "whole-page image description"),
    ("loop", "periodic repetition loop the de-loop would still collapse"),
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
                    print(f"    [!] NOT OK: {r['ref_id']} ({len(r.get('failed_pages', []))} failed page(s))")
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

    leak_total = sum(len(members(c)) for c, _ in _LEAK_CATS)

    print("\n=== LEAKS (investigate / bring up) ===")
    if leak_total == 0:
        print("  none — no commentary, descriptions, or periodic loops leaked.")
    else:
        for cat, desc in _LEAK_CATS:
            m = members(cat)
            print(f"  [{len(m):4d}] {desc}")
            for pg in m[: args.show]:
                print(f"        {sample(pg)}  ({pg['bytes']}B)")
                if cat == "commentary":
                    for ln in pg["flags"]["commentary"][:3]:
                        print(f"           ! {ln[:90]}")
                else:
                    print(f"           {pg['text'][:110]!r}")

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
                        "commentary_lines", "description", "loop", "dup", "cjk", "empty"])
            for pg in pages:
                fl = pg["flags"]
                w.writerow([pg["ref_id"], pg["path"].name, pg["bytes"], pg["lines"],
                            round(pg["uniq"] * 100), len(fl["commentary"]),
                            int(fl["description"]), int(fl["loop"]),
                            int(fl["dup"]), int(fl["cjk"]), int(fl["empty"])])
        print(f"\nWrote per-page CSV: {args.csv}")

    print(f"\nVerdict: {leak_total} leak(s) across {len(txts)} page(s). "
          + ("Investigate the LEAKS above." if leak_total else "Clean on the high-signal checks."))
    return 1 if leak_total else 0


if __name__ == "__main__":
    raise SystemExit(main())
