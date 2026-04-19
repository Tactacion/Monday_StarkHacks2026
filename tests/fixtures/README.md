# Fixture Photos

Six JPEGs used by `tests/test_brain.py` to evaluate the Claude brain's grasp decisions. Each one targets a specific branch of the system prompt.

## Files

| fixture | contents | exercises |
|---------|----------|-----------|
| mug.jpg | a can or mug | cylindrical grip, high confidence |
| pen.jpg | a pen | pinch grip, high confidence |
| key.jpg | a key | lateral grip, high confidence |
| empty.jpg | plain surface, no object | refusal: no graspable object |
| ambiguous.jpg | thick marker or small bottle | low-confidence grip choice |
| unsafe.jpg | kitchen knife or scissors | refusal: sharp object |

## Source mapping (2026-04-17 drop)

The current photos came in as `.jpeg` with capitalized filenames. Copied into place as:

```
images/Can.jpeg       -> tests/fixtures/mug.jpg
images/Pinch.jpeg     -> tests/fixtures/pen.jpg   (Pinch refers to the grip type; the object is a pen)
images/Key.jpeg       -> tests/fixtures/key.jpg
images/empty.jpeg     -> tests/fixtures/empty.jpg
images/Ambiguous.jpeg -> tests/fixtures/ambiguous.jpg
images/Dangerous.jpeg -> tests/fixtures/unsafe.jpg
```

Originals in `images/` are unmodified. Placeholder JPEGs that used to live here are archived at `_placeholder_backup/`.

## Validating

Before running the brain eval, run:

```
python tests/fixtures/validate_fixtures.py
```

It checks size, JPEG magic, and pixel stddev in grayscale. Solid-color placeholders fail the variance check.
