"""Run the same installed CLI exposed by the console entry point."""

from .interfaces.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
