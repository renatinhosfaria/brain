# Task 2 report: validated remote/domain models

## RED evidence

Added `tests/test_meta_ads_models.py` before production implementation. The
focused test run failed because `brain.meta_ads_models` did not yet exist:

```text
ModuleNotFoundError: No module named 'brain.meta_ads_models'
```

## GREEN evidence

Implemented `src/brain/meta_ads_models.py`, then ran:

```text
uv run python -m unittest discover -s tests -p 'test_meta_ads_models.py' -v
Ran 7 tests ... OK
uv run ruff check src/brain/meta_ads_models.py tests/test_meta_ads_models.py
All checks passed!
uv run python -m unittest discover -s tests
Ran 521 tests in 52.309s
OK
```

The model boundary now provides the exact read-tool and error-code sets,
frozen validated value objects, exact source-ID preservation from the raw
camelCase Observer shape, configured account normalization, active-only
confirmation, and safe code-only `MetaAdsError` stringification.

## Changed files

- `src/brain/meta_ads_models.py`
- `tests/test_meta_ads_models.py`

## Self-review

- IDs accept only 1–64 ASCII decimal digits and preserve leading zeroes.
- Names/statuses reject null/empty values, control characters, invalid Unicode,
  and values over 512 UTF-8 bytes.
- Raw parsing never hashes, reconstructs, or consults alternate source fields.
- Confirmation retains ordinary and effective statuses while requiring both
  effective statuses to be `ACTIVE`.
- Error messages cannot include remote response content or arbitrary codes.
- No live credentials, remote repository, or Task 1 files were touched.

## Concerns

The system Python lacks project dependencies (`mcp`, `starlette`), so the
direct full-suite invocation cannot import three existing test modules. The
project `uv` environment contains the dependencies and passed the complete
521-test suite; this is the reported authoritative run.
