#!/usr/bin/env python3
"""End-to-end smoke test for discovery, authority, and waveform queries."""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import tempfile


ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests/fixtures"
CLI = ROOT / "scripts/wave_debug.py"


def run(*arguments: str) -> str:
    result = subprocess.run(
        [sys.executable, str(CLI), *arguments],
        cwd=ROOT,
        check=True,
        text=True,
        capture_output=True,
    )
    return result.stdout


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="sv-waveform-debug-") as temporary:
        output = Path(temporary)
        common = ("--workspace", str(FIXTURE), "--out-dir", str(output))
        inspected = run("inspect", *common)
        assert "selected-top: top_tb" in inspected

        run("authority", *common, "--force")
        packet_path = Path(
            run(
                "packet",
                *common,
                "--window",
                "0",
                "--window-len",
                "10",
                "--focus-scope",
                "TOP.top_tb.u_dut",
            ).strip()
        )
        packet = json.loads(packet_path.read_text(encoding="utf-8"))
        signals = packet["focus_signals"]
        assert len(signals) == 3
        assert all(signal["rtl"]["match_status"] == "exact" for signal in signals)

        point = json.loads(
            run(
                "signal",
                *common,
                "--signal",
                "TOP.top_tb.u_dut.valid_o",
                "--time",
                "16",
                "--window-len",
                "10",
            )
        )
        assert point["value_at_time"]["value"] == "1"
        assert point["value_at_time"]["t"] == 15

    print("smoke test passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
