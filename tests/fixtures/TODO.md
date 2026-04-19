# Fixture Photos Needed

Replace these placeholder solid-color JPEGs with real photos.

Each photo should be:
- Well-lit
- Single object against a plain (white or light grey) background
- Shot from roughly the same angle as the demo camera (slightly above, looking down)
- 640x480 minimum resolution

## Required photos

| File | Object | Notes |
|------|--------|-------|
| mug.jpg | A ceramic mug or cup | Handle visible, upright |
| pen.jpg | A ballpoint pen or pencil | Lying flat, full length visible |
| key.jpg | A house key | Flat on surface, teeth visible |
| empty.jpg | Empty table surface | No objects in frame at all |
| ambiguous.jpg | A thick marker or highlighter | Could be cylindrical or pinch grip — intentionally ambiguous |
| unsafe.jpg | A kitchen knife | Blade visible — used to test refusal path |

## How to take the photos

Put the object on a white sheet of paper under good indoor lighting.
Hold your phone directly above, pointing straight down.
No shadows across the object.

The current placeholder files are solid-color JPEGs created with PIL.
They will make Claude guess randomly — replace them before running test_brain.py for real results.

## Status

Placeholders replaced with real photos on 2026-04-17. Originals preserved at `tests/fixtures/_placeholder_backup/` for comparison. See `tests/fixtures/README.md` for the source filename mapping.
