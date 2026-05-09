from __future__ import annotations

PYTHON_BOOTSTRAP_POLICY = """Python bootstrap policy:
- For the initial plan of a Python repository, first ensure a usable runtime and common native build tools exist before creating the venv. Prefer an install command like `apt-get update && apt-get install -y python3 python3-venv python3-pip python3-dev build-essential pkg-config`.
- Do not guess between `python` and `python3` after `command not found`. If `python3` is missing, install the Python runtime package; if only `python` is missing, use explicit `python3` or `.bootstrap/venv/bin/python`.
- For compiled Python projects, consider common build packages up front when evidence suggests they are needed, instead of repeatedly changing interpreter names. Examples include `cython3`, `meson`, `ninja-build`, `gfortran`, `libopenblas-dev`, and `liblapack-dev`.
- Create and use `.bootstrap/venv` for Python project dependencies.
- Prefer editable project installs such as `.bootstrap/venv/bin/python -m pip install -e .` when the repository is being bootstrapped as a development checkout. The verifier should simulate a real second-developer environment, not just a published wheel install.
- Non-editable installs such as `.bootstrap/venv/bin/python -m pip install .` are allowed when editable mode is unsupported, not documented, or repeatedly fails for project-specific reasons.
- For compiled Python projects, especially meson-python/Cython projects, install persistent build tools/dependencies into `.bootstrap/venv` first and consider editable installs with `--no-build-isolation`, for example `.bootstrap/venv/bin/python -m pip install --no-build-isolation -e .`, so later imports do not depend on temporary pip build-environment paths.
- If build isolation hides required build tools, install build dependencies into `.bootstrap/venv` first and use `--no-build-isolation`.
- If install succeeds but Python import verification fails from `/workspace/repo/<package>/...`, consider source-tree shadowing: the verifier runs from the repo root, so Python may import the source tree instead of the installed site-packages package. For non-editable installs, run import checks from outside the repo, for example `cd /tmp && /workspace/repo/.bootstrap/venv/bin/python -c "import package"`. Do not run pytest against `/workspace/repo/<package>` after a non-editable install; that reintroduces the source tree on `sys.path`.
- Container paths such as `/workspace/repo` are allowed when changing cwd away from the repo for import checks. Do not use host paths such as `runs/...`.
- For meson-python repairs that need venv tools on PATH, use an absolute container path such as `PATH=/workspace/repo/.bootstrap/venv/bin:$PATH ...`; do not rely on relative PATH entries like `.bootstrap/venv/bin` when build subprocesses may change cwd.
- Keep each command reason consistent with the command itself. If the reason says PATH is set, the command must set PATH; if the reason says non-editable install, the command must not use `-e` or `--editable`.
"""
