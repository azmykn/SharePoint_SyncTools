"""Project root: parent of the `sp_sync` package (repository root when run from checkout)."""

import os

_PKG_DIR = os.path.dirname(os.path.abspath(__file__))


def project_root() -> str:
    return os.path.dirname(_PKG_DIR)
