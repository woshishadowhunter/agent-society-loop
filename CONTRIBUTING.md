# Contributing

Thanks for helping make agent orchestration more testable and transparent.

## Setup

```bash
git clone https://github.com/woshishadowhunter/agent-society-loop.git
cd agent-society-loop
python -m pip install -e .
python -m unittest discover -s tests -v
```

## Change process

1. Open an issue for behavior changes or new public interfaces.
2. Create a focused branch from `main`.
3. Add a failing test that demonstrates the required behavior.
4. Implement the smallest coherent change and keep the full suite green.
5. Update English and Chinese documentation when commands or user behavior change.
6. Run `python -m compileall -q src examples` and `git diff --check`.
7. Open a pull request describing the problem, approach, evidence, and limitations.

Do not commit API keys, generated databases, virtual environments, or model outputs containing private data. New provider adapters must redact credentials from exceptions and include offline tests.

## Design principles

- Preserve explicit lifecycle transitions and bounded execution.
- Keep execution and quality judgment in separate components.
- Record enough evidence to explain routing and completion.
- Do not market planned features as implemented behavior.
- Prefer standard-library solutions unless a dependency removes substantial complexity.

