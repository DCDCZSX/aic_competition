# Task 2 Report — Bound preview-token memory

## Status

Implemented the requested in-process preview-token capacity policy without changing unrelated Phase 2 areas.

## Changes

- Extended `PreviewTokenService` with optional `per_owner_limit` and `global_limit` constructor arguments.
- Preserved existing callers by defaulting limits to 5 tokens per owner and 5000 tokens globally.
- Kept expiration purge, capacity enforcement, and dictionary access under the existing lock.
- On issuance, purged expired entries first, then evicted the deterministic oldest entries for the owner and globally as needed.
- Kept owner binding and one-time consumption behavior unchanged.
- Wired `APP_PREVIEW_MAX_PER_USER` and `APP_PREVIEW_MAX_TOTAL` in `backend/app.py`.
- Added both variables with defaults to `.env.example`.
- Added focused service tests for per-owner eviction, global eviction, owner binding, and one-time consumption.

## TDD evidence

The new focused tests were run before production code was added and failed with the expected missing constructor API:

```text
TypeError: PreviewTokenService.__init__() got an unexpected keyword argument 'per_owner_limit'
Ran 3 tests
FAILED (errors=3)
```

After implementation:

```text
python -m unittest backend.tests.test_preview_tokens -v
Ran 3 tests
OK
```

## Verification

- Focused preview-token and security regression tests: 15 passed.
- Full backend suite, run from `backend/` as required by existing relative template-path tests:

```text
python -m unittest discover -s tests -p 'test*.py' -v
Ran 558 tests
OK
```

Running the same discovery command from the repository root is not a valid invocation for this suite because an existing frontend test resolves `templates/home.html` relative to the current directory; that invocation produced one `FileNotFoundError`, and the correctly rooted rerun passed.

## Concerns

None for the requested Task 2 scope.

## Follow-up hardening round

The review finding was confirmed against commit `01f5d69`: direct `int(os.getenv(...))` calls in `backend/app.py` caused startup failure for empty or non-numeric preview-limit variables, while `PreviewTokenService` converted zero and negative limits to `1` instead of the documented safe defaults.

The fix adds explicit positive-integer normalization in `PreviewTokenService`:

- invalid, empty, zero, and negative per-owner values use `5`;
- invalid, empty, zero, and negative global values use `5000`;
- valid positive values are preserved;
- app wiring passes raw environment values to the robust service constructor, avoiding unchecked preview-limit parsing during import.

New tests were written and run red before the fix. They covered `None`, empty, non-numeric, zero, and negative direct-construction values, plus invalid and valid app wiring. The red run showed the expected `TypeError`/`ValueError`, incorrect `1` clamping, and app-import failure.

After the fix:

```text
python -m unittest tests.test_preview_tokens -v
Ran 7 tests
OK

python -m unittest discover -s tests -p 'test*.py' -v
Ran 562 tests
OK
```

No additional concerns identified.
