"""Entry point for `python -m citadel_platform.identity` (see cli.py's own
module docstring for why this file has to exist at all -- the same reasoning
applies here as it does for citadel_platform.migrations's identical file)."""

from __future__ import annotations

from citadel_platform.identity.cli import main

raise SystemExit(main())
