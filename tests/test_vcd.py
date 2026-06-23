#!/usr/bin/env python3
from __future__ import annotations

from pathlib import Path
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from wave_debug_lib.project import module_candidates, parse_filelist
from wave_debug_lib.vcd import Timescale, iter_changes, parse_time, read_header
from wave_debug_lib.analysis import compare_waveforms
from wave_debug_lib.wave import open_waveform


VCD = """$timescale
  1 ps
$end
$scope module top $end
$var wire 1 ! clk $end
$var wire 4 # data [3:0] $end
$var wire 1 $ \\escaped.name $end
$upscope $end
$enddefinitions $end
#0
x!
b10xz #
z$
#10
1!
b0011 #
"""


class VcdTests(unittest.TestCase):
    def test_header_time_and_unknown_values(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "wave.vcd"
            path.write_text(VCD, encoding="utf-8")
            header = read_header(path)
            self.assertEqual(header.timescale, Timescale(1, "ps"))
            self.assertEqual([signal.path for signal in header.signals], [
                "top.clk", "top.data", "top.\\escaped.name",
            ])
            self.assertEqual(parse_time("1.5ns", header.timescale), 1500)
            self.assertEqual(list(iter_changes(path, {"!", "#", "$"}))[:3], [
                (0, "!", "x"), (0, "#", "10xz"), (0, "$", "z"),
            ])

    def test_unaligned_time_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "not aligned"):
            parse_time("1ps", Timescale(10, "ps"))

    def test_filelist_and_top_discovery(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "rtl").mkdir()
            (root / "rtl/top.sv").write_text(
                "module leaf; endmodule\nmodule chip;\n  leaf u();\nendmodule\n",
                encoding="utf-8",
            )
            (root / "files.f").write_text(
                "+incdir+rtl/include +define+TRACE -Irtl/extra -DDEBUG rtl/top.sv\n",
                encoding="utf-8",
            )
            manifest = parse_filelist(root / "files.f")
            self.assertEqual(manifest.defines, ["TRACE", "DEBUG"])
            self.assertEqual(len(manifest.include_dirs), 2)
            self.assertEqual(module_candidates(manifest.files), ["chip"])

    def test_compare_uses_physical_time_across_timescales(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            good = root / "good.vcd"
            bad = root / "bad.vcd"
            original = (ROOT / "tests/fixtures/wave.vcd").read_text(encoding="utf-8")
            good.write_text(original, encoding="utf-8")
            converted = original.replace("$timescale 1ns $end", "$timescale 1ps $end")
            for timestamp in (20, 15, 10, 5, 0):
                converted = converted.replace(f"#{timestamp}\n", f"#{timestamp * 1000}\n")
            bad.write_text(converted, encoding="utf-8")
            left = open_waveform(good, ROOT, root / "out")
            right = open_waveform(bad, ROOT, root / "out")
            result = compare_waveforms(left, right, None, [], 64)
            self.assertIsNone(result["first_divergence"])


if __name__ == "__main__":
    unittest.main()
