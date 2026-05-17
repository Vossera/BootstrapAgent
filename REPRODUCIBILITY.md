# Reproducibility Guide

This guide describes how to check the released artifact and rerun small benchmark slices from a fresh clone.

## Environment

Required:

- Python 3.11 or newer.
- Git.
- Docker, for deterministic verification.
- Network access, if cloning benchmark repositories or using hosted LLM providers.

Optional for agent-backed runs:

- `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `GOOGLE_API_KEY`, or `DEEPSEEK_API_KEY`, depending on `--model`.
- `RETHINK_LLM_MODEL`, if you prefer setting the default model through the environment.

## Install and Smoke Test

```bash
cd code
python -m pip install -e '.[dev]'
python -m pytest
```

The test suite exercises discovery, bootstrap manifest generation, CLI helpers, verifier result handling, safety checks, and deterministic fallback behavior. It does not require external LLM calls.

## Docker Verifier

Build the base verifier image:

```bash
cd code
scripts/build-bootstrap-base-image.sh
```

The default image tag is `rethink-bootstrap-base:latest`, matching `RuntimeConfig.docker_image`. Override it with:

```bash
scripts/build-bootstrap-base-image.sh my-image:tag
```

## Single-Repository Run

Generate `.bootstrap/` for a local repository without Docker verification:

```bash
cd code
rethink bootstrap --repo /path/to/repo --out runs/manual --allow-fallback --no-verify
```

Generate and verify with Docker:

```bash
rethink bootstrap --repo /path/to/repo --out runs/manual-verified --allow-fallback
```

For hosted LLM-backed runs, install optional dependencies and provide a model/API key:

```bash
python -m pip install -e '.[agents,dev]'
OPENAI_API_KEY=... rethink bootstrap --repo /path/to/repo --out runs/openai --model openai:gpt-4o-mini
```

## Benchmark Inputs

The repository includes compact CSV input lists under `data/benchmark_inputs/`:

- `repo2run_selected_122.csv`: 122 selected GitHub repositories.
- `executionAgent.csv`: 50 baseline input repositories.
- `installmatic.csv`: 40 baseline input repositories.

Clone benchmark repositories into a local cache:

```bash
cd code
python scripts/clone_benchmark_repos.py ../data/benchmark_inputs/repo2run_selected_122.csv --out /tmp/rethink-benchmark
```

Run a small slice:

```bash
python scripts/run_csv_slice.py ../data/benchmark_inputs/repo2run_selected_122.csv 1 5 \
  --benchmark /tmp/rethink-benchmark \
  --out runs/repo2run-1-5 \
  --allow-fallback \
  --no-verify
```

Remove `--no-verify` after building the Docker image if you want deterministic verification. Remove `--allow-fallback` and pass `--model` for LLM-backed generation.

## Expected Outputs

Each successful run writes:

- `workspace/repo/.bootstrap/`: generated setup, doctor, verify, commands, evidence, and context files.
- `evaluation_log.json`: status, maturity, trace summary, command counts, timing, and token usage when available.
- `agent_outputs/`: structured planning and repair outputs.
- `traces/`: verifier command traces when verification is enabled.

Generated `runs/` directories are intentionally ignored by Git. Preserve or archive them separately when preparing paper tables or long-running benchmark reports.
