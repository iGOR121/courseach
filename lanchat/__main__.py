"""Entry point for ``python -m lanchat``."""

from .app import cli

if __name__ == "__main__":
    raise SystemExit(cli())
