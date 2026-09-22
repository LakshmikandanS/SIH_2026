"""Entry point for `python -m citadel_platform.migrations` (cli.py's own module
docstring and its `argparse` `prog=` both document this invocation -- without this
file, `python -m citadel_platform.migrations` fails with "No module named
citadel_platform.migrations.__main__" instead, regardless of anything else being
correct. See citadel_platform.identity.__main__ for the identical fix applied there."""

from __future__ import annotations

from citadel_platform.migrations.cli import main

raise SystemExit(main())
