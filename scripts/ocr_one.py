"""OCR a single TIF through the real engine + preprocessing, for tuning.

The CLI processes a whole Ref ID; when tuning layout/word-box behavior you
often want to iterate on *one* page. This runs exactly the per-page path
``pipeline.process_ref_id`` uses — ``preprocess()`` with the engine's
``adjust_preprocess`` overrides, then ``engine.ocr(...)`` — so the ALTO it
writes is byte-for-byte what a batch would produce for that page. Pair it
with ``scripts/overlay_words.py`` to inspect box placement.

Usage::

    python scripts/ocr_one.py SOURCE.tif OUT_DIR
    python scripts/ocr_one.py SOURCE.tif OUT_DIR --page-index 100
    python scripts/ocr_one.py SOURCE.tif OUT_DIR --engine tesseract

Writes ``OUT_DIR/<stem>.xml`` and ``OUT_DIR/<stem>.txt``. Run from the repo
root so ``src.alto_ocr`` resolves.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

# Allow running as `python scripts/ocr_one.py` from the repo root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.alto_ocr.glmocr import (  # noqa: E402
    GLMOCRConfig, GLMOCRError, check_endpoint, resolve_endpoint,
)
from src.alto_ocr.line_detect import DetectParams  # noqa: E402
from src.alto_ocr.ocr import GLMOCREngine, OCROptions, TesseractEngine, find_tesseract  # noqa: E402
from src.alto_ocr.preprocess import PreprocessConfig, preprocess  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ocr_one", description=__doc__)
    parser.add_argument("source", type=Path, help="source TIF/PNG/JPEG")
    parser.add_argument("out_dir", type=Path, help="directory to write <stem>.xml + <stem>.txt")
    parser.add_argument("--engine", default="glmocr", choices=("glmocr", "tesseract"))
    parser.add_argument("--page-index", type=int, default=1,
                        help="1-based Page ID / PHYSICAL_IMG_NR to stamp (default: 1)")
    parser.add_argument("--glmocr-model", default=None,
                        help="override the glm-ocr model tag (default: GLMOCRConfig default)")
    parser.add_argument("--min-dpi", type=int, default=300)
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(levelname)s %(name)s: %(message)s")

    if args.engine == "glmocr":
        cfg = GLMOCRConfig(endpoint=resolve_endpoint(None),
                           **({"model": args.glmocr_model} if args.glmocr_model else {}))
        # Fail fast with a clear message if Ollama/the model isn't reachable,
        # mirroring main._build_engine — otherwise a down endpoint surfaces as a
        # cryptic per-page timeout deep in the OCR loop.
        try:
            check_endpoint(cfg)
        except GLMOCRError as e:
            print(f"glmocr endpoint check failed: {e}", file=sys.stderr)
            return 2
        engine = GLMOCREngine(config=cfg, detect_params=DetectParams())
    else:
        engine = TesseractEngine(binary=find_tesseract(None), options=OCROptions())

    pre_cfg = engine.adjust_preprocess(PreprocessConfig(min_dpi=args.min_dpi))
    pre = preprocess(args.source, pre_cfg)

    args.out_dir.mkdir(parents=True, exist_ok=True)
    base = args.out_dir / args.source.stem
    alto, txt = engine.ocr(
        pre.image, base,
        dpi=pre.effective_dpi, page_index=args.page_index,
        source_filename=args.source.name,
    )
    print(f"stages={pre.stages_applied} dpi={pre.effective_dpi}")
    print(f"ALTO -> {alto}")
    print(f"TXT  -> {txt}  ({txt.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
