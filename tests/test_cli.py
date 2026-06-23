from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "scripts/wave_debug.py"
FIXTURE = ROOT / "tests/fixtures/wave.vcd"


def invoke(*arguments: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(CLI), *arguments],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=check,
    )


class CliTests(unittest.TestCase):
    def test_waveform_only_probe_is_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            shutil.copy2(FIXTURE, root / "wave.vcd")
            inspected = json.loads(invoke("inspect", "--workspace", str(root), "--json").stdout)
            self.assertEqual(inspected["source"]["files"], 0)
            result = json.loads(
                invoke(
                    "probe", "--workspace", str(root), "--scope", "top_tb.u_dut",
                    "--start", "0", "--end", "20", "--max-changes", "2",
                ).stdout
            )
            self.assertTrue(result["truncated"])
            self.assertEqual(len(result["changes"]), 2)
            self.assertEqual(result["signals"][0]["rtl"]["reason"], "authority-not-provided")

    def test_metadata_cache_invalidates_with_waveform(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            wave = root / "wave.vcd"
            shutil.copy2(FIXTURE, wave)
            common = ("inspect", "--workspace", str(root), "--out-dir", "out", "--json")
            invoke(*common)
            cache = root / "out/cache/waveform_meta"
            self.assertEqual(len(list(cache.iterdir())), 1)
            wave.write_text(wave.read_text(encoding="utf-8") + "\n", encoding="utf-8")
            invoke(*common)
            self.assertEqual(len(list(cache.iterdir())), 2)

    def test_ambiguous_top_requires_explicit_choice(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "two.sv").write_text("module alpha; endmodule\nmodule beta; endmodule\n", encoding="utf-8")
            result = invoke(
                "authority", "--workspace", str(root), "--waveform", str(FIXTURE),
                "--out-dir", str(root / "out"), check=False,
            )
            self.assertEqual(result.returncode, 2)
            self.assertIn("top module is ambiguous", result.stderr)

    def test_invalid_time_has_actionable_error(self) -> None:
        result = invoke(
            "signal", "--workspace", str(ROOT / "tests/fixtures"), "--waveform", str(FIXTURE),
            "--signal", "top_tb.u_dut.valid_o", "--time", "half-a-cycle", check=False,
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("use ticks or a value such as 42ns", result.stderr)

    def test_protocol_probe_preserves_reset_x_and_payload_violation(self) -> None:
        waveform = ROOT / "tests/fixtures/protocol_bad.vcd"
        result = json.loads(
            invoke(
                "probe", "--workspace", str(ROOT / "tests/fixtures"),
                "--waveform", str(waveform), "--scope", "handshake_tb",
                "--start", "0ns", "--end", "20ns", "--max-changes", "40",
            ).stdout
        )
        changes = {(row["time"]["ticks"], row["signal"], row["value"]) for row in result["changes"]}
        self.assertIn((0, "handshake_tb.rst_n", "x"), changes)
        self.assertIn((10, "handshake_tb.valid", "1"), changes)
        self.assertIn((15, "handshake_tb.data", "00100010"), changes)


if __name__ == "__main__":
    unittest.main()
