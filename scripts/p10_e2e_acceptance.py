"""P10 real end-to-end probe/replay entry point.

P10 intentionally reuses the P9 transport harness so both acceptance phases
exercise the exact same public sync and SSE contracts.  The artifact is a
separate, append-only, redacted JSONL file for handoff and audit.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import p9_e2e_acceptance as harness  # noqa: E402  # isort: skip


harness.ARTIFACT = ROOT / "memory" / "p10-e2e-acceptance.jsonl"
harness.PROBE_LABEL = "p10"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=("probe", "replay"), required=True)
    args = parser.parse_args()
    harness.probe() if args.mode == "probe" else harness.replay()


if __name__ == "__main__":
    main()
