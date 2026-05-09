# Repository Guidelines

## Project Structure & Module Organization

This is a Python 3.11+ package using a `src/` layout. Core package code lives in `src/rethink/`. The command-line entry point is `src/rethink/cli.py`, with orchestration in `src/rethink/agents/`, bootstrap file generation in `src/rethink/bootstrap/`, verification logic in `src/rethink/verifier/`, and batch evaluation in `src/rethink/evaluation/`. Tests are in `tests/`, currently centered on `tests/test_core.py`. Docker verifier support is under `docker/bootstrap-base/`, helper scripts are in `scripts/`, and generated run artifacts go under `runs/`.

## Build, Test, and Development Commands

- `python -m pip install -e '.[dev]'`: install the package locally with test dependencies.
- `python -m pip install -e '.[agents,dev]'`: install optional LLM agent dependencies plus dev tools.
- `python -m pytest`: run the full test suite.
- `rethink bootstrap --repo <path-or-url> --out runs/manual --allow-fallback --no-verify`: generate a `.bootstrap` package without Docker verification.
- `rethink verify --repo <repo-with-bootstrap>`: run the deterministic Docker verifier for an existing `.bootstrap`.
- `scripts/build-bootstrap-base-image.sh`: build the Docker base image used by verification.

## Coding Style & Naming Conventions

Use modern Python with type annotations and `from __future__ import annotations`, matching the existing modules. Prefer `pathlib.Path` for filesystem work and Pydantic models for structured data. Use 4-space indentation, snake_case for functions and variables, PascalCase for classes, and uppercase only for true constants. Keep comments sparse and focused on non-obvious behavior.

## Testing Guidelines

Tests use `pytest` while the current suite is written with `unittest.TestCase`. Add tests in `tests/` with filenames matching `test_*.py` and test methods named `test_*`. Prefer temporary directories over repository fixtures for generated files. For verifier or Docker-related changes, include tests that assert command traces, status values, and failure behavior where practical.

## Commit & Pull Request Guidelines

This repository currently has no commit history, so use concise imperative commit messages such as `Add verifier trace serialization` or `Fix bootstrap fallback status`. Pull requests should include a short problem statement, implementation summary, test results, and any Docker or external-service requirements. Link related issues when available and include screenshots or logs only when they clarify CLI output or generated artifacts.

## Security & Configuration Tips

Do not commit generated `runs/` output, secrets, or local environment files. Treat commands embedded in generated bootstrap files carefully: doctor commands should remain read-only, while install commands may mutate only the target bootstrap environment.
