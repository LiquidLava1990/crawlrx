"""Console entry point for the challenge-gateway service."""
from __future__ import annotations

import os
import warnings

try:
    from .core.sentinel_errors import ErrCode  # noqa: F401
except ImportError:
    class _E:
        def __getattr__(self: object, _: object) -> object: return "ERR_UNKNOWN"
    ErrCode = _E()  # type: ignore[assignment]

warnings.filterwarnings('ignore')
os.environ['PYTHONWARNINGS'] = 'ignore'
os.environ['TF_CPP_MIN_LOG_LEVEL'] = '3'

# CRITICAL: Pre-import Go/C extensions BEFORE torch loads.
# tls_client (Go via ctypes) crashes if imported after torch on Windows.
try:
    import tls_client  # noqa: F401
except ImportError:
    pass  # optional dependency; solver registration degrades gracefully

import uvicorn

from .main import app


def main() -> None:
    """Run the service via uvicorn (console-script and `python -m` target)."""
    from .config import get_config

    cfg = get_config()
    uvicorn.run(app, host=cfg.host, port=cfg.port, log_level=cfg.log_level.lower())


if __name__ == "__main__":
    main()
