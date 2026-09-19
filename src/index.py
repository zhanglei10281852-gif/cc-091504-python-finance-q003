from __future__ import annotations

import os

from app import create_server
from equity.service import EquityService


def main() -> None:
    host = os.getenv("HOST", "0.0.0.0")
    port = int(os.getenv("PORT", "8000"))
    runtime_dir = os.getenv("RUNTIME_DIR", ".runtime")
    service = EquityService(runtime_dir=runtime_dir)
    server = create_server(host, port, service)
    server.serve_forever()


if __name__ == "__main__":
    main()
