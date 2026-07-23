"""Expose benchmark cross-split duplicate auditing as a standalone check."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from gepase.benchmarks.audit import audit_leakage


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("manifest", type=Path)
    arguments = parser.parse_args()
    result = audit_leakage(Path.cwd(), arguments.manifest)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["valid"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
