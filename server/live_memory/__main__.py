"""Entry point: `python -m live_memory` (or the `live-memory-server` script).

Builds the server from the environment (Config) and serves the MCP tool over
HTTP (streamable-http) plus the /health, /stats, /notify routes.
"""
from __future__ import annotations

import logging

from .config import Config
from .logging_setup import configure_logging
from .server import build_server


def main() -> None:
    configure_logging()
    cfg = Config()
    log = logging.getLogger("live_memory")
    log.info("Live Memory starting on http://%s:%d/mcp (model=%s, endpoint=%s, data=%s)",
             cfg.host, cfg.port, cfg.model, cfg.base_url, cfg.data_dir)
    mcp = build_server(cfg)
    # Streamable-HTTP serves the MCP endpoint at /mcp and the custom routes at root.
    try:
        mcp.run(transport="streamable-http")
    except KeyboardInterrupt:
        log.info("Live Memory stopped (interrupted).")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass  # clean exit on Ctrl-C (anyio re-raises it from the runner)
