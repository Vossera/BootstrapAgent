# Bootstrap Context

Repo: BootsrapAgent

Detected languages: Python

Package managers: python/pyproject

Install commands:
- `apt-get update && apt-get install -y python3 python3-venv python3-pip python3-dev build-essential pkg-config`
- `python3 -m venv .bootstrap/venv`
- `.bootstrap/venv/bin/python -m pip install -U pip wheel`
- `.bootstrap/venv/bin/python -m pip install -e "code[dev]"`

Minimal validation: `.bootstrap/venv/bin/python -m compileall code`

Strongest local CI-derived validation: `.bootstrap/venv/bin/python -m pytest code/tests`

CI workflows inspected: none
