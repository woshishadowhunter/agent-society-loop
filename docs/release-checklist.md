# v1.0 Release Checklist

Use this checklist before creating or promoting a release.

## Local Verification

```bash
python -m pip install -e .
python -W error::ResourceWarning -m unittest discover -s tests -q
python -m compileall -q src tests examples
seed-society product self-test --json
seed-society scheduler self-test --json
seed-society a2a self-test --json
```

All commands must exit with status 0. PostgreSQL-specific tests may skip only
when no local PostgreSQL URL is configured.

## Package Verification

```bash
python -m build
```

Install the built wheel into a fresh virtual environment and run:

```bash
seed-society product self-test --json
seed-society demo --db demo.db
seed-society scheduler self-test --json
```

## CI Gates

Required GitHub Actions:

- Python 3.10
- Python 3.11
- Python 3.12
- Python 3.13
- Python 3.14
- PostgreSQL 17
- Container smoke test

Do not publish a stable release while any required gate is failing.

## Release Metadata

- `pyproject.toml` version is `1.0.0`.
- `seed_society.__version__` is `1.0.0`.
- `CHANGELOG.md` includes the release date and product readiness summary.
- `README.md` and `README.zh-CN.md` show `seed-society product self-test`.
- `docs/production-runbook.md` and `docs/deployment.md` describe the supported
  production boundary.
- Git tag is `v1.0.0`.
- GitHub Release includes wheel and source distribution artifacts.

## Release Notes Template

```markdown
Seed Society v1.0.0 is the first stable product release.

Highlights:
- Product readiness self-test: `seed-society product self-test --json`
- Stable CLI/package metadata for the v1 line
- Production runbook for PostgreSQL, local model workers, health checks, recovery, and outbox delivery
- Existing v0.10 local-model worker and transactional outbox runtime promoted to the stable support boundary

Verification:
- Full unit suite passed
- Product readiness self-test passed
- Wheel installed in a clean environment and passed smoke tests
```
