"""`python -m citadel_platform.identity` -- operator-facing session-identity
tooling. Thin, matching `citadel_platform.migrations.cli`'s own stance: the
logic lives in `citadel_platform.identity.keys`, tested there without argparse
or stdout in the way; this module is the command line around it.
"""

from __future__ import annotations

import argparse
import sys
from typing import Optional, Sequence

from citadel_platform.identity.keys import ENV_VAR, generate_key_material


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m citadel_platform.identity")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser(
        "genkey",
        help=f"print a freshly generated {ENV_VAR} value; store it, do not commit it",
    )

    args = parser.parse_args(argv)

    if args.command == "genkey":
        print(generate_key_material())
        return 0

    print(f"error: unknown command {args.command!r}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
