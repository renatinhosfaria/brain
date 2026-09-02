# Task 1 report: domain types and fail-safe configuration

## Status

DONE. Commit: `451537bd941f8063030b93bb1d2790af83587c61` (`feat: define Meta Ads attribution settings`).

## TDD evidence

- RED: `PYTHONPATH=src .venv/bin/python -m unittest tests.test_meta_ads_models -v` failed with `ModuleNotFoundError: brain.meta_ads_models` before implementation.
- GREEN: `PYTHONPATH=src .venv/bin/python -m unittest tests.test_meta_ads_models tests.test_config -q` passed, 26 tests.
- Static checks: `ruff format` on all touched Python files and `ruff check` on those files passed.
- `git diff --check` and commit validation passed.

## Files

- `src/brain/meta_ads_models.py`: pinned account normalization, strict CTWA source eligibility, immutable ad/capability/view records, bounded error codes, and confirmed/pending CEO payload builders.
- `src/brain/config.py`: fail-safe Meta attribution settings, strict environment parsing, RFC3339 expiry conversion, token `repr` suppression, and range validation.
- `tests/test_meta_ads_models.py`: identifier boundaries, eligibility, bounded errors, validation, and payload shape coverage.
- `tests/test_config.py`: defaults, enabled environment settings, token redaction, and expiry coverage.

## Concerns

- The Meta token is intentionally accepted only from `BRAIN_META_ADS_MCP_ACCESS_TOKEN`; missing or expired credentials do not prevent Brain construction when attribution is enabled.
- The worktree contains a pre-existing untracked `.venv`; it was not included in the commit.
- Network/MCP behavior is intentionally deferred to later tasks.
