from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
import sys

sys.path.insert(0, str(ROOT / "scripts"))

from wave_debug_lib.rtl_authority import build_rtl_authority, parse_modules


class AuthorityTests(unittest.TestCase):
    def test_parameterized_ansi_ports_and_hierarchy(self) -> None:
        source = """
module leaf #(parameter int WIDTH = 8) (
  input logic [WIDTH-1:0] data_i, mask_i,
  output logic valid_o
);
  logic [WIDTH/2-1:0] partial;
endmodule

module top;
  logic [7:0] data;
  logic [7:0] mask;
  logic valid;
  leaf #(.WIDTH(8)) u_leaf (.data_i(data), .mask_i(mask), .valid_o(valid));
endmodule
"""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            rtl = root / "design.sv"
            rtl.write_text(source, encoding="utf-8")
            output = root / "authority"
            build_rtl_authority([rtl], "top", output)

            with sqlite3.connect(output / "rtl_authority.sqlite3") as connection:
                rows = {
                    row[0]: row[1:]
                    for row in connection.execute(
                        "select full_signal_name, direction, decl_width_bits, module_type from authority_lookup"
                    )
                }
            self.assertEqual(rows["top.u_leaf.data_i"], ("input", 8, "leaf"))
            self.assertEqual(rows["top.u_leaf.mask_i"], ("input", 8, "leaf"))
            self.assertEqual(rows["top.u_leaf.valid_o"], ("output", 1, "leaf"))
            self.assertEqual(rows["top.u_leaf.partial"][1], 4)
            self.assertEqual(rows["top.data"][1], 8)
            self.assertEqual(rows["top.mask"][1], 8)

            table = json.loads((output / "rtl_authority_table.json").read_text(encoding="utf-8"))
            self.assertEqual(table["top"], "top")
            self.assertTrue(table["signals"])

    def test_non_ansi_ports_and_recursive_instantiation_are_bounded(self) -> None:
        source = """
module old_style(clk, value);
  input clk;
  output [3:0] value;
  wire [3:0] value;
  old_style nested(clk, value);
endmodule
"""
        with tempfile.TemporaryDirectory() as temporary:
            rtl = Path(temporary) / "old.v"
            rtl.write_text(source, encoding="utf-8")
            modules = parse_modules([rtl])
            self.assertEqual({signal.name for signal in modules["old_style"].signals}, {"clk", "value"})
            output = Path(temporary) / "out"
            build_rtl_authority([rtl], "old_style", output)
            with sqlite3.connect(output / "rtl_authority.sqlite3") as connection:
                count = connection.execute("select count(*) from authority_lookup").fetchone()[0]
                direction = connection.execute(
                    "select direction from authority_lookup where full_signal_name = 'old_style.value'"
                ).fetchone()[0]
            self.assertEqual(count, 2)
            self.assertEqual(direction, "output")

    def test_unknown_top_is_actionable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            rtl = Path(temporary) / "only.sv"
            rtl.write_text("module actual; endmodule\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "top module is not declared"):
                build_rtl_authority([rtl], "missing", Path(temporary) / "out")


if __name__ == "__main__":
    unittest.main()
