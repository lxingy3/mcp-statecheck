"""Run the package-owned real SDK client transport matrix."""

import sys

from mcp_statecheck.cli import main

if __name__ == "__main__":
    raise SystemExit(main(["matrix", *sys.argv[1:]]))
