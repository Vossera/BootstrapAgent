# Rethink Bootstrap Code

This package implements the CLI, planning logic, bootstrap writer, Docker verifier, benchmark helpers, and tests for the Rethink Bootstrap arXiv artifact.

## Install

Run commands from this `code/` directory unless noted otherwise.

```bash
python -m pip install -e '.[dev]'
```

For agent-backed bootstrap generation:

```bash
python -m pip install -e '.[agents,dev]'
```

## Test

```bash
python -m pytest
```

## Docker Verifier Image

```bash
scripts/build-bootstrap-base-image.sh
```

The default image tag is `rethink-bootstrap-base:latest`.

## CLI Examples

Generate a bootstrap package without Docker verification:

```bash
rethink bootstrap --repo <path-or-url> --out runs/manual --allow-fallback --no-verify
```

Verify an existing repository containing `.bootstrap/`:

```bash
rethink verify --repo <repo-with-bootstrap>
```

Clone benchmark repositories listed in a CSV:

```bash
python scripts/clone_benchmark_repos.py ../data/benchmark_inputs/repo2run_selected_122.csv --out /tmp/rethink-benchmark
```

Run a benchmark slice:

```bash
python scripts/run_csv_slice.py ../data/benchmark_inputs/repo2run_selected_122.csv 1 3 --benchmark /tmp/rethink-benchmark --out runs/smoke --allow-fallback --no-verify
```

## Important Modules

- `src/rethink/cli.py`: command-line interface.
- `src/rethink/agents/`: LLM-agent orchestration, prompts, tools, and validation helpers.
- `src/rethink/bootstrap/`: `.bootstrap` file generation.
- `src/rethink/verifier/`: deterministic Docker verifier, execution traces, and policy checks.
- `src/rethink/evaluation/`: batch evaluation support.
- `scripts/clone_benchmark_repos.py`: local benchmark checkout helper.
- `scripts/run_csv_slice.py`: row-slice benchmark runner.

Scripts may require access to local benchmark checkouts, Docker, or external LLM APIs depending on the experiment.
