from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from src.pipeline import run_pipeline_from_config


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Satellite image downloader pipeline (config-driven)."
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("config/config.yaml"),
        help="Path to YAML config file.",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )

    try:
        summary = run_pipeline_from_config(args.config)
    except Exception as exc:
        logging.getLogger(__name__).error("Pipeline failed: %s", exc, exc_info=True)
        return 1

    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
