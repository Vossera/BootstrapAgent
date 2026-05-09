# Artifact Data

This directory contains compact benchmark input lists for the arXiv companion artifact. It does not contain raw run directories, cloned repositories, logs, or derived paper tables.

## `benchmark_inputs/`

- `repo2run_selected_122.csv`: 122 selected repositories used for the main benchmark slice.
- `executionAgent.csv`: 50 repositories used for the ExecutionAgent baseline input list.
- `installmatic.csv`: 40 repositories used for the Installamatic baseline input list.

The CSV files use stable columns such as `name`, `safe_name`, `url`, and `language`. `repo2run_selected_122.csv` also includes `index` and `full_name`.

## Local Preparation

From the repository root:

```bash
cd code
python scripts/clone_benchmark_repos.py ../data/benchmark_inputs/repo2run_selected_122.csv --out /tmp/rethink-benchmark
```

Then run a row slice with:

```bash
python scripts/run_csv_slice.py ../data/benchmark_inputs/repo2run_selected_122.csv 1 5 \
  --benchmark /tmp/rethink-benchmark \
  --out runs/repo2run-1-5 \
  --allow-fallback \
  --no-verify
```

Generated outputs are written under `code/runs/` by default and are intentionally ignored by Git.
