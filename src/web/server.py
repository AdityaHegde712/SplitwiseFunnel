"""Launch the local Splitwise Funnel UI on loopback only."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
from typing import Sequence

import uvicorn

from src.web.app import create_app


def parse_arguments(argv: Sequence[str] | None = None) -> argparse.Namespace:
    """Parse local server options without exposing a host override."""
    parser = argparse.ArgumentParser(description="Run the local Splitwise Funnel UI.")
    parser.add_argument("--config", type=Path, default=Path("data/household.json"))
    parser.add_argument("--port", type=int, default=8765)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    """Serve only localhost, keeping household data off the network."""
    arguments = parse_arguments(argv)
    app = create_app(
        config_path=arguments.config,
        log_directory=_default_log_directory(),
    )
    uvicorn.run(app, host="127.0.0.1", port=arguments.port, log_level="warning")
    return 0


def _default_log_directory() -> Path:
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        return Path(local_app_data) / "SplitwiseFunnel" / "logs"
    return Path.home() / "AppData" / "Local" / "SplitwiseFunnel" / "logs"


if __name__ == "__main__":
    raise SystemExit(main())
