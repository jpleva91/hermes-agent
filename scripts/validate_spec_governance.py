#!/usr/bin/env python3
"""Validate a Spec Governance sidecar and print JSON status."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from plugins.kanban.governance import status_payload, validate_governance_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate a Spec Kit governance.yaml sidecar.")
    parser.add_argument("path", type=Path, help="Spec directory or governance.yaml path")
    args = parser.parse_args(argv)

    result = validate_governance_path(args.path)
    print(json.dumps(status_payload(result), indent=2, ensure_ascii=False, sort_keys=True))
    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
