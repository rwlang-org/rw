# Contributing to rw

Thank you for considering a contribution to rw. This document walks through the flow from setting up your development environment to opening a PR, along with the coding conventions.

## 1. About this project

rw is a Python-flavored, async-first, statically-typed compiled language.

- **Frontend**: implemented in Python (lexer / parser / sema / irgen). The code lives under `rwc/`.
- **Runtime**: implemented in C. The code lives under `runtime/`.
- **Primary source for the language spec**: see the Markdown documents under `docs/specs/`.

Whenever you have a question about the spec or the design, we recommend checking `docs/specs/` first.

## 2. Setting up the development environment

### Prerequisites

- Python 3.11 or later
- [uv](https://github.com/astral-sh/uv)
- clang (with C11 support)
- make

### Steps

```sh
git clone https://github.com/ryuichi1208/rw
cd rw
uv sync --extra dev
make -C runtime
```

`uv sync --extra dev` fetches all of the Python dependencies used for development. `make -C runtime` builds the C runtime.

## 3. Testing

### Python side

To run the full test suite:

```sh
uv run pytest -v
```

To run just a single test file:

```sh
uv run pytest tests/test_irgen.py -v
```

### C runtime

```sh
make -C runtime test
```

### E2E samples

The E2E tests are structured by compiling `examples/*.rw` with `rwc` and comparing the results against `*.rw.expected`. When you add a new language feature, please add the corresponding sample and expected file.

## 4. lint / format (pre-commit)

This project enforces formatting and linting with [pre-commit](https://pre-commit.com/).

First-time setup:

```sh
pip install pre-commit  # or: uv tool install pre-commit
pre-commit install
```

Run against all files:

```sh
pre-commit run --all-files
```

Tools in use:

- **ruff** (lint + format)
- **black**
- **mypy**

The configuration lives in `pyproject.toml` and `.pre-commit-config.yaml`. Since we plan to tighten mypy incrementally, existing code may still have outstanding errors; for any new code you write, please make sure the types pass cleanly.

## 5. PR flow

1. Create a feature branch. Naming examples:
   - `feat/issue-NN-short-desc`
   - `fix/issue-NN-short-desc`
   - `docs/issue-NN-short-desc`
2. Keep your changes small and split them into focused commits.
3. Follow the PR template, and reference the related issue in the body using the `Closes #N` form.
4. Once CI passes, request a review.
5. Merges into main are done with **squash merge** by default.
6. Releases are handled by [tagpr](https://github.com/Songmu/tagpr), which automatically generates a release PR triggered by pushes to main. To release, you just merge that PR.

## 6. Commit message convention

We follow a (loose) Conventional Commits style.

```
<type>: <subject>

<optional body>
```

### Examples of type

- `feat`: a new feature
- `fix`: a bug fix
- `docs`: documentation-only changes
- `chore`: miscellaneous changes (build config, etc.)
- `ci`: changes to CI configuration
- `refactor`: a refactor that does not change behavior
- `test`: adding or fixing tests
- `perf`: a performance improvement

### Rules for subject

- 50 characters or fewer
- Imperative mood (e.g. start with `add`, `fix`, `update`)
- No trailing period

When you write a body, wrap it at roughly 72 characters.

## 7. Coding conventions

### Python (`rwc/`)

- Line length is 120 characters
- Format with ruff + black; pre-commit formats automatically
- Type hints are recommended. Write new modules so they pass mypy cleanly
- Import ordering follows ruff's `I` rule (isort-compatible)

### C (`runtime/`)

- Assumes C11
- Indentation is 4 spaces
- Match the existing style on a per-file basis
- Public APIs are consolidated in `runtime.h`
- For fiber / scheduler-related implementation, see `docs/specs/05-fibers.md` and `docs/specs/06-scheduler-mn.md`

## 8. Documentation

- For changes that touch the language spec, add a new `.md` under `docs/specs/` or update an existing file
- Before landing a large change, write down the design or plan in `docs/plans/YYYY-MM-DD-<topic>.md` and open a PR based on it; this makes discussion easier

## 9. Choosing an issue

- Issues labeled `good first issue` are a good place to get started
- Before you begin, comment on the issue to request assignment. This avoids duplicated work

---

If you have any questions or suggestions, feel free to reach out via an issue or a PR.
