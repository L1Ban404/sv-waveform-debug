#!/usr/bin/env python3
"""Investigate FST/VCD waveforms together with Verilog/SystemVerilog source."""

from __future__ import annotations

from pathlib import Path
import sys


SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from wave_debug_lib.cli import main


if __name__ == "__main__":
    raise SystemExit(main())
