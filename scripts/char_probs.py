#!/usr/bin/env python3
"""Print what the character verifier thinks of each glyph on a plate image.

    python scripts/char_probs.py data/generated/plate_1_DL8CBF4890.png

For every character the OCR read, this prints the grammar-restricted answer
(the one `_fuse_chars` would act on), the unrestricted top-3, and flags the
positions where the two disagree - which is exactly the case the verifier
exists for. Segment count is printed too, because a mismatch is the reason
`run_ocr` skips verification rather than guessing.

Read-only: nothing here feeds the pipeline, it is a way to look at the model's
opinion without attaching a debugger to a live WebSocket frame.
"""

from __future__ import annotations

import sys
from pathlib import Path

import cv2

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "backend"))

from ai import char_grammar, char_segment, char_verifier  # noqa: E402
from app.services.plate_reader import read_crop  # noqa: E402


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: char_probs.py <plate image>")
        return 2
    path = Path(sys.argv[1])
    if not path.exists():
        print("not found", path)
        return 2
    img = cv2.imread(str(path))
    if img is None:
        print("unreadable image", path)
        return 2

    read = read_crop(img)
    if read is None:
        print("the OCR could not read this crop")
        return 1
    # Same compaction run_ocr does: RapidOCR's spaces are not plate characters.
    text = "".join(ch for ch in str(read.text or "").upper() if ch.isalnum())
    print(f"OCR: {read.text!r}  conf={read.conf:.3f}  variant={read.best_variant}")
    print(f"compacted: {text!r} ({len(text)} characters)")

    if not char_verifier.is_enabled():
        print(f"verifier unavailable: {char_verifier.unavailable_reason()}")
        return 1

    crops = char_segment.segment_chars(img, text)
    print(f"segmented {len(crops)}/{len(text)} glyphs")
    if len(crops) != len(text):
        # Not an error worth a traceback: run_ocr skips verification here too.
        return 1

    results = char_verifier.classify_batch(crops)
    probs = [r["probs"] if r else None for r in results]
    grammar = char_grammar.decode(text, probs)
    shape = grammar.pattern or "(no legal shape for this length)"
    print(f"template: {shape}  state={grammar.state or '-'}  ({grammar.reason})")

    for i, (ch, res) in enumerate(zip(text, results)):
        if res is None:
            print(f"  {i}: {ch}  VERIFIER_NA")
            continue
        allowed = grammar.allowed[i] if grammar.ok else None
        cls_c, cls_p = char_verifier.restricted_max(res, allowed)
        top3 = ", ".join(f"{c}={p:.3f}" for c, p in res["top3"])
        flag = "" if cls_c == ch else "   <-- grammar disagrees with the OCR"
        print(f"  {i}: {ch}  restricted={cls_c}({cls_p:.3f})  top3: {top3}{flag}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
