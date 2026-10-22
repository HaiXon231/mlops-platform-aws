"""Render checked-in Kubernetes templates with explicit environment values."""

import os
import sys
from pathlib import Path
from string import Template


def main() -> int:
    if len(sys.argv) != 2:
        raise SystemExit("usage: m23-render.py TEMPLATE")
    print(Template(Path(sys.argv[1]).read_text(encoding="utf-8")).substitute(os.environ), end="")
    return 0


if __name__ == "__main__":
    sys.exit(main())
