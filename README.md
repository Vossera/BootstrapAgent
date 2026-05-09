# Rethink Bootstrap

Rethink Bootstrap is the public companion repository for an arXiv preprint on agent-ready startup packages for software repositories. It contains a compact Python implementation, deterministic verifier, benchmark input lists, and reproducibility instructions for generating and checking `.bootstrap/` packages.

The repository is intentionally small. Generated run directories, local repository checkouts, raw logs, paper build products, API keys, and machine-specific caches are excluded so the artifact can be cloned, inspected, and tested without private state.

## Repository Layout

- `code/`: Python package, CLI, Docker verifier, benchmark runner, and tests.
- `data/benchmark_inputs/`: CSV inputs used to select benchmark repositories.
- `REPRODUCIBILITY.md`: environment setup, smoke tests, Docker verifier setup, and benchmark rerun notes.
- `CITATION.cff`: citation metadata placeholder to update once the arXiv identifier is assigned.
- `LICENSE`: MIT license for the released code and documentation.

## Quick Start

```bash
cd code
python -m pip install -e '.[dev]'
python -m pytest
```

Optional LLM-agent dependencies can be installed with:

```bash
python -m pip install -e '.[agents,dev]'
```

Generate a `.bootstrap/` package for a local repository without Docker verification:

```bash
rethink bootstrap --repo /path/to/repo --out runs/manual --allow-fallback --no-verify
```

Build the Docker verifier image from `code/`:

```bash
scripts/build-bootstrap-base-image.sh
```

Then verify a repository that already contains `.bootstrap/`:

```bash
rethink verify --repo /path/to/repo-with-bootstrap
```

## Benchmark Inputs

The included CSV files are compact benchmark input lists, not full benchmark outputs. To prepare local benchmark checkouts:

```bash
cd code
python scripts/clone_benchmark_repos.py ../data/benchmark_inputs/repo2run_selected_122.csv --out /tmp/rethink-benchmark
```

To run a small row slice:

```bash
python scripts/run_csv_slice.py ../data/benchmark_inputs/repo2run_selected_122.csv 1 3 \
  --benchmark /tmp/rethink-benchmark \
  --out runs/repo2run-slice \
  --allow-fallback \
  --no-verify
```

Full benchmark reproduction may require Docker, network access, local storage for cloned repositories, and provider API keys for non-fallback LLM runs. See `REPRODUCIBILITY.md` for the longer protocol.

## Citation

If you use this artifact, cite the associated arXiv preprint. Update `CITATION.cff` with the final author list, arXiv identifier, repository URL, and release DOI once those are available.
