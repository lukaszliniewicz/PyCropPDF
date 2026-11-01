"""Run the repository's static checks with the active Python environment."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    # Keep CLI behavior identical locally and in CI: checks must not rewrite the
    # type baseline. Run basedpyright directly to prune resolved baseline entries.
    environment = dict(os.environ, CI="true")
    checks: tuple[tuple[str, ...], ...] = (
        ("-m", "ruff", "check", "."),
        ("-m", "ruff", "format", "--check", "."),
        ("-m", "basedpyright", "--pythonpath", sys.executable),
        ("-m", "vulture"),
        ("-c", "from importlinter.cli import lint_imports_command; lint_imports_command()"),
    )
    for arguments in checks:
        print(f"\n> {sys.executable} {' '.join(arguments)}", flush=True)
        result = subprocess.run(
            [sys.executable, *arguments], cwd=root, env=environment, check=False
        )
        if result.returncode:
            return result.returncode
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
