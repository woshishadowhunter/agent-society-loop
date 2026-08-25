# Open Source Growth Sprint Design

## Objective

Turn three recently published repositories into a coherent, low-friction open
source portfolio that can be discovered, tried, evaluated, and contributed to
by people who do not already know the maintainer.

The repositories have different jobs:

- Seed Society is the flagship governed multi-agent runtime.
- Alaya Protocol is the narrow, reusable experience-memory entry point.
- Lianxin Medicine Garden is the real-world community operations case study.

This sprint improves adoption surfaces. It does not add another agent feature,
change production behavior, or claim adoption that has not happened.

## Success Criteria

1. Both Python projects build valid wheels and expose a release-triggered PyPI
   Trusted Publishing workflow with no stored PyPI token.
2. A new visitor can understand each repository's problem, differentiator, and
   first successful action from the first README screen.
3. Seed Society publishes a reproducible, deterministic evidence command
   and clearly distinguishes measured behavior from future benchmarks.
4. Alaya has a GitHub-detectable Apache-2.0 license and concrete framework-neutral
   integration guidance.
5. Lianxin has one local validation command and a privacy-safe evaluation path
   before an operator configures WeChat Cloud.
6. All three repositories expose Discussions and scoped newcomer issues.
7. No README asks for stars before showing value, and no metric is fabricated.

## Public Experience

Each README follows the same conversion sequence:

1. literal one-sentence outcome;
2. visual or terminal proof;
3. installation or evaluation command;
4. explanation of the differentiator and limits;
5. links to deeper architecture, security, and contribution material.

Seed Society keeps its detailed operational documentation, but moves it
below the quick start and evidence sections. Alaya leads with the experience
seed lifecycle and a five-minute command. Lianxin leads with screenshots and a
local, synthetic-data evaluation path.

## Distribution

The Python repositories use PyPI Trusted Publishing from a protected GitHub
environment named `pypi`. The workflow builds once, checks package metadata,
and publishes only for a GitHub release or an explicit manual dispatch. Initial
PyPI project ownership still requires the maintainer's PyPI account; the
repository never stores an API token.

Lianxin remains a source distribution because WeChat Mini Programs are imported
through WeChat Developer Tools. A root `package.json` exposes validation and
demo-data commands without converting the application to an npm package.

## Evidence and Community

Evidence must be reproducible. Existing deterministic self-tests and evaluation
fixtures are surfaced as the first proof for Seed Society. Broader public
comparisons against other frameworks remain a follow-up issue until a neutral
benchmark corpus and methodology exist.

Discussions are enabled for questions, use cases, and design proposals. Each
repository receives small, verifiable issues suitable for external contributors.
Issues must describe acceptance criteria and must not be placeholders for core
maintainer work.

## Safety and Scope

- Existing product branches and unmerged user work remain untouched.
- PyPI upload is attempted only after package ownership and trusted publisher
  configuration can be verified.
- No automated social posting, unsolicited messaging, fake stars, or reciprocal
  engagement is used.
- Lianxin demonstrations use synthetic records and no real family data.
- Repository metadata changes are applied only after the corresponding PR is
  merged, so links never point at unavailable content.

## Verification

- Seed Society: full `unittest` suite, compile, build, metadata check,
  wheel install, deterministic self-test.
- Alaya Protocol: full `unittest` suite, compile, build, metadata check, wheel
  install and CLI smoke test.
- Lianxin: JavaScript syntax validation, JSON parsing, demo-data generation and
  output schema checks through the root validation command.
- GitHub: all PR checks pass; Discussions and issue URLs are verified through
  the GitHub API.
