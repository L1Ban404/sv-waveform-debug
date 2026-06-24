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
        [sys.executable, str(CLI), *arguments], cwd=ROOT, text=True,
        capture_output=True, check=check,
    )


def hypothesis(identifier: str, expected: list[dict[str, object]], falsification: list[dict[str, object]], required: list[str]) -> dict[str, object]:
    return {
        "id": identifier,
        "description": f"test {identifier}",
        "required_signals": required,
        "expected_checks": expected,
        "falsification_checks": falsification,
        "confidence": "low",
        "status": "active",
    }


class CaseTests(unittest.TestCase):
    def _init_case(self, root: Path) -> Path:
        case_path = root / "case.json"
        invoke(
            "case", "init", "--workspace", str(root), "--waveform", "wave.vcd", "--out", str(case_path),
            "--symptom", "observable mismatch", "--symptom-start", "0ns", "--symptom-end", "20ns",
            "--affected-signal", "top_tb.u_dut.valid_o",
        )
        return case_path

    def test_init_requires_explicit_waveform(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            shutil.copy2(FIXTURE, root / "wave.vcd")
            result = invoke("case", "init", "--workspace", str(root), check=False)
            self.assertEqual(result.returncode, 2)
            self.assertIn("requires an explicit --waveform", result.stderr)

    def test_validate_all_predicates_and_write_immutable_revision(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            shutil.copy2(FIXTURE, root / "wave.vcd")
            case_path = self._init_case(root)
            case = json.loads(case_path.read_text(encoding="utf-8"))
            valid = "top_tb.u_dut.valid_o"
            clock = "top_tb.u_dut.clk"
            case["hypotheses"] = [
                hypothesis("value", [{"id": "at", "kind": "value_at", "signal": valid, "time": "16ns", "equals": "1"}], [{"id": "not_zero", "kind": "value_at", "signal": valid, "time": "16ns", "equals": "0"}], [valid]),
                hypothesis("stable", [{"id": "hold", "kind": "stable", "signal": valid, "start": "0ns", "end": "10ns", "equals": "0"}], [], [valid]),
                hypothesis("transition", [{"id": "rise", "kind": "transition", "signal": valid, "start": "0ns", "end": "20ns", "from": "0", "to": "1"}], [], [valid]),
                hypothesis("edge", [{"id": "clock_rise", "kind": "edge", "signal": clock, "start": "0ns", "end": "20ns", "edge": "rising"}], [], [clock]),
                hypothesis("ordered", [{"id": "before", "kind": "occurs_before", "start": "0ns", "end": "20ns", "first": {"kind": "edge", "signal": clock, "edge": "rising"}, "second": {"kind": "value", "signal": valid, "equals": "1"}}], [], [clock, valid]),
                hypothesis("falsified", [], [{"id": "contradiction", "kind": "value_at", "signal": valid, "time": "16ns", "equals": "1"}], [valid]),
                hypothesis("missing", [], [], ["top_tb.no_such_signal"]),
            ]
            case_path.write_text(json.dumps(case, indent=2) + "\n", encoding="utf-8")
            snapshot = root / "revision.json"
            report = root / "report.md"
            invoke("case", "validate", "--case", str(case_path), "--out", str(snapshot), "--report", str(report))
            original = json.loads(case_path.read_text(encoding="utf-8"))
            result = json.loads(snapshot.read_text(encoding="utf-8"))
            self.assertEqual(original["revision"], 0)
            self.assertTrue(all(item["status"] == "active" for item in original["hypotheses"]))
            statuses = {item["id"]: item["status"] for item in result["hypotheses"]}
            for identifier in ("value", "stable", "transition", "edge", "ordered"):
                self.assertEqual(statuses[identifier], "supported")
            self.assertEqual(statuses["falsified"], "contradicted")
            self.assertEqual(statuses["missing"], "insufficient-evidence")
            validation = result["validation_history"][-1]
            self.assertTrue(validation["provenance_fingerprint"])
            evidence = [Path(path) for item in validation["results"] for path in item.get("evidence", [])]
            self.assertTrue(evidence)
            packet = json.loads(evidence[0].read_text(encoding="utf-8"))
            self.assertEqual(packet["query"]["sampling_phase"], "waveform-observed")
            self.assertIn(next(iter(packet["series"].values()))["before"], {"0", "1"})
            text = report.read_text(encoding="utf-8")
            self.assertIn("`top_tb.u_dut.valid_o`", text)
            self.assertIn("## Validation", text)
            self.assertIn("## Hypothesis", text)

    def test_unaligned_and_truncated_checks_are_insufficient(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            shutil.copy2(FIXTURE, root / "wave.vcd")
            case_path = self._init_case(root)
            case = json.loads(case_path.read_text(encoding="utf-8"))
            valid = "top_tb.u_dut.valid_o"
            case["hypotheses"] = [
                hypothesis("unaligned", [{"id": "unaligned", "kind": "value_at", "signal": valid, "time": "1ps", "equals": "0"}], [], [valid]),
                hypothesis("truncated", [{"id": "rise", "kind": "transition", "signal": valid, "start": "0ns", "end": "20ns", "from": "0", "to": "1"}], [], [valid]),
            ]
            case_path.write_text(json.dumps(case, indent=2) + "\n", encoding="utf-8")
            unaligned = root / "unaligned.json"
            invoke("case", "validate", "--case", str(case_path), "--hypothesis", "unaligned", "--out", str(unaligned))
            result = json.loads(unaligned.read_text(encoding="utf-8"))
            self.assertEqual(result["hypotheses"][0]["status"], "insufficient-evidence")
            truncated = root / "truncated.json"
            invoke("case", "validate", "--case", str(case_path), "--hypothesis", "truncated", "--max-changes", "1", "--out", str(truncated))
            result = json.loads(truncated.read_text(encoding="utf-8"))
            self.assertEqual(result["hypotheses"][1]["status"], "insufficient-evidence")

    def test_changed_waveform_provenance_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            wave = root / "wave.vcd"
            shutil.copy2(FIXTURE, wave)
            case_path = self._init_case(root)
            case = json.loads(case_path.read_text(encoding="utf-8"))
            case["hypotheses"] = [hypothesis("empty", [], [], ["top_tb.u_dut.valid_o"])]
            case_path.write_text(json.dumps(case, indent=2) + "\n", encoding="utf-8")
            wave.write_text(wave.read_text(encoding="utf-8") + "\n", encoding="utf-8")
            result = invoke("case", "validate", "--case", str(case_path), check=False)
            self.assertEqual(result.returncode, 2)
            self.assertIn("case provenance mismatch", result.stderr)

    def test_init_rejects_a_manifest_for_another_waveform(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            shutil.copy2(FIXTURE, root / "current.vcd")
            shutil.copy2(FIXTURE, root / "other.vcd")
            manifest = root / "other.json"
            invoke("provenance", "--workspace", str(root), "--waveform", "other.vcd", "--out", str(manifest))
            result = invoke(
                "case", "init", "--workspace", str(root), "--waveform", "current.vcd",
                "--provenance-file", str(manifest), check=False,
            )
            self.assertEqual(result.returncode, 2)
            self.assertIn("does not match --waveform", result.stderr)


if __name__ == "__main__":
    unittest.main()
