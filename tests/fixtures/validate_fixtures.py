"""
tests/fixtures/validate_fixtures.py

Sanity check the six fixture photos before running the brain eval. Catches
the common regression of a solid-color placeholder JPEG sneaking in and
producing a bogus eval pass (Claude refuses every time because there is no
object, and the refusal-expected cases happen to line up).

Checks per fixture:
  1. File exists.
  2. File size above PLACEHOLDER_SIZE_CEILING (placeholders were ~6 KB,
     real phone photos are well above 50 KB).
  3. Valid JPEG magic (0xFF 0xD8 at the start).
  4. Pixel stddev in grayscale above VARIANCE_FLOOR. Solid-color fill scores
     near 0. Real photos, even of plain surfaces, score well above the floor.

Exit 0 on all pass, 1 on any fail.

Run: python tests/fixtures/validate_fixtures.py
"""
from __future__ import annotations

import statistics
import sys
from pathlib import Path
from typing import Optional

from PIL import Image


FIXTURE_DIR = Path(__file__).resolve().parent
EXPECTED = [
    "mug.jpg",
    "pen.jpg",
    "key.jpg",
    "empty.jpg",
    "ambiguous.jpg",
    "unsafe.jpg",
]

PLACEHOLDER_SIZE_CEILING = 10 * 1024  # 10 KB
VARIANCE_FLOOR = 15.0                  # stddev over grayscale pixels


def check_one(path: Path) -> tuple[bool, str]:
    if not path.is_file():
        return False, "missing"

    size = path.stat().st_size
    if size <= PLACEHOLDER_SIZE_CEILING:
        return False, f"size {size/1024:.1f} KB under {PLACEHOLDER_SIZE_CEILING/1024:.0f} KB floor"

    with path.open("rb") as f:
        head = f.read(2)
    if head != b"\xff\xd8":
        return False, f"bad JPEG magic {head!r}"

    try:
        with Image.open(path) as im:
            gray = im.convert("L")
            pixels = list(gray.getdata())
    except Exception as e:
        return False, f"PIL open failed: {e}"

    if len(pixels) < 2:
        return False, "fewer than 2 pixels"

    stddev = statistics.pstdev(pixels)
    if stddev < VARIANCE_FLOOR:
        return False, f"stddev {stddev:.1f} under {VARIANCE_FLOOR:.0f} floor (likely placeholder)"

    return True, f"size={size/1024:.0f} KB  stddev={stddev:.1f}  dims={im.size}"


def main() -> int:
    print(f"\nValidating fixtures in {FIXTURE_DIR}")
    print(f"  size floor:     {PLACEHOLDER_SIZE_CEILING/1024:.0f} KB")
    print(f"  variance floor: stddev >= {VARIANCE_FLOOR:.0f}")
    print("-" * 72)

    results: list[tuple[str, bool, str]] = []
    for name in EXPECTED:
        ok, detail = check_one(FIXTURE_DIR / name)
        results.append((name, ok, detail))
        tag = "PASS" if ok else "FAIL"
        print(f"  {name:<16}  {tag}   {detail}")

    print("-" * 72)
    passed = sum(1 for _, ok, _ in results if ok)
    total = len(results)
    print(f"  {passed} / {total} fixtures pass")
    print()
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
