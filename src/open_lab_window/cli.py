"""``olw``: serve the Window."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .archive import Library
from .server import DEFAULT_PORT, make_server


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="olw", description="Read-only browser view of recorded Open Laboratory runs."
    )
    commands = parser.add_subparsers(dest="command", required=True)
    serve = commands.add_parser(
        "serve", help="serve the Window on the loopback address (there is no other choice)"
    )
    serve.add_argument(
        "--data-root",
        type=Path,
        help="data root holding runs/ (default: $OPEN_LAB_DATA, else open-lab-data in the home "
        "directory)",
    )
    serve.add_argument("--port", type=int, default=DEFAULT_PORT)
    serve.add_argument("--verbose", action="store_true", help="log each request to stderr")
    arguments = parser.parse_args(argv)

    library = Library(arguments.data_root)
    try:
        server = make_server(library, arguments.port, quiet=not arguments.verbose)
    except OSError as error:
        print(f"olw: cannot listen on port {arguments.port}: {error}", file=sys.stderr)
        return 1
    host, port = server.server_address[:2]
    if not library.runs_root.is_dir():
        print(f"olw: no runs directory at {library.runs_root} yet", file=sys.stderr)
    print(f"Window (read-only) at http://{host}:{port}/  -  Ctrl+C to stop", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
