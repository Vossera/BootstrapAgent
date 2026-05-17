# BootstrapAgent: Distilling Repository Setup into Reusable Agent Knowledge 

This repository contains the official code for the paper:

> **BootstrapAgent: Distilling Repository Setup into Reusable Agent Knowledge**

> 📄 Paper link: 

---

## 📌 Overview
Code agents increasingly help developers work with unfamiliar repositories, but every such task depends on a costly prerequisite: bootstrapping the repository into a usable development state. This process requires substantial trial-and-error exploration, yet the resulting knowledge--resolved dependencies, repair strategies--stays trapped in a single conversation, unavailable to future agents. We therefore formulate repository bootstrapping as a reusable startup knowledge problem and introduce **BootstrapAgent**, a multi-agent framework that distills the heuristics discovered during bootstrap exploration into a persistent, verifiable, agent-consumable *.bootstrap* contract. Through evidence extraction, structured planning, deterministic Docker-based verification, and trace-driven repair, BootstrapAgent generates a contract covering environment setup, diagnostic checks, minimal verification, and accumulated repair knowledge. We further propose *warm repair with clean replay* to accelerate iterative debugging without sacrificing cold-start reproducibility, and a *delta repair with sanity check* to prevent reward hacking. Experiments on three benchmarks show that BootstrapAgent achieves a 92.9% success rate, outperforming the baseline by over 10% while reducing downstream agent token usage by 25.9% and build time by 22.3%.

---

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

