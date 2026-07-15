import sys

import mcp_statecheck


def test_package_uses_pinned_python() -> None:
    assert sys.version_info[:2] == (3, 12)
    assert mcp_statecheck.__version__ == "0.1.0.dev0"
