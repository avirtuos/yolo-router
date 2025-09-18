"""
Module entrypoint for `python -m yolo_router`.
Delegates to the CLI main in yolo_router.cli.
"""

from .cli import main

if __name__ == "__main__":
    raise SystemExit(main())
