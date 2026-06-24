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
GOOD = ROOT / "tests/fixtures/wave.vcd"
BAD = ROOT / "tests/fixtures/wave_bad.vcd"


def invoke(*arguments: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, str(CLI), *arguments], cwd=ROOT, text=True, capture_output=True, check=check)


def write_junit(path: Path, failed: bool = False) -> None:
    body = '<failure message="failed at 15ns">failed at 15ns</failure>' if failed else ""
    path.write_text(f'<testsuite tests="1"><testcase classname="suite" name="case">{body}</testcase></testsuite>', encoding="utf-8")


class WorkflowTests(unittest.TestCase):
    def _reproduce(self, root: Path, source: Path, target: str, results: Path, exit_code: int, failure_time: str | None = None) -> Path:
        target_path = root / target
        command = f"cp {source} {target_path}; exit {exit_code}"
        args = [
            "reproduce", "--workspace", str(root), "--waveform", target, "--run-command", command,
            "--results", str(results), "--out", str(root / f"{target}.manifest.json"), "--testcase", "case",
        ]
        if failure_time:
            args.extend(("--failure-time", failure_time))
        result = invoke(*args, check=False)
        self.assertEqual(result.returncode, exit_code)
        return root / f"{target}.manifest.json"

    def test_reproduce_archives_confirmed_failure_and_around_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            results = root / "results.xml"
            write_junit(results, failed=True)
            manifest_path = self._reproduce(root, GOOD, "failure.vcd", results, 1, "15ns")
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(manifest["failure"]["relation"], "confirmed-failure")
            self.assertEqual(manifest["failure"]["time"], "15ns")
            self.assertEqual(manifest["junit"]["failure_time_candidates"], ["15ns"])
            probe = json.loads(invoke(
                "probe", "--workspace", str(root), "--waveform", "failure.vcd", "--provenance-file", str(manifest_path),
                "--around-failure", "--radius", "2ns", "--scope", "top_tb.u_dut",
            ).stdout)
            self.assertEqual(probe["window"]["start"]["ticks"], 13)
            summary = json.loads(invoke("inspect", "--workspace", str(root), "--waveform", "failure.vcd", "--json").stdout)
            self.assertNotIn("provenance_detail", summary)
            verbose = json.loads(invoke("inspect", "--workspace", str(root), "--waveform", "failure.vcd", "--json", "--verbose").stdout)
            self.assertIn("provenance_detail", verbose)
            unconfirmed = root / "unconfirmed.json"
            invoke("provenance", "--workspace", str(root), "--waveform", "failure.vcd", "--out", str(unconfirmed))
            rejected = invoke(
                "probe", "--workspace", str(root), "--waveform", "failure.vcd", "--provenance-file", str(unconfirmed),
                "--around-failure", "--radius", "2ns", "--scope", "top_tb.u_dut", check=False,
            )
            self.assertEqual(rejected.returncode, 2)
            self.assertIn("confirmed-failure", rejected.stderr)

    def test_case_confirmation_and_fix_outcomes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            shutil.copy2(BAD, root / "failure.vcd")
            case_path = root / "case.json"
            rejected = invoke("case", "init", "--workspace", str(root), "--waveform", "failure.vcd", "--out", str(case_path), check=False)
            self.assertEqual(rejected.returncode, 2)
            self.assertIn("confirmed failing waveform", rejected.stderr)
            invoke("case", "init", "--workspace", str(root), "--waveform", "failure.vcd", "--out", str(case_path), "--confirm-failure")
            case = json.loads(case_path.read_text(encoding="utf-8"))
            signal = "top_tb.u_dut.valid_o"
            case["hypotheses"] = [{
                "id": "behavior", "description": "output should be asserted", "required_signals": [signal],
                "expected_checks": [{"id": "expected", "kind": "value_at", "signal": signal, "time": "15ns", "equals": "1"}],
                "falsification_checks": [{"id": "failure", "kind": "value_at", "signal": signal, "time": "15ns", "equals": "0"}],
                "confidence": "low", "status": "active",
            }]
            case_path.write_text(json.dumps(case, indent=2) + "\n", encoding="utf-8")
            before = root / "before.json"
            invoke("case", "validate", "--case", str(case_path), "--out", str(before))
            self.assertEqual(json.loads(before.read_text(encoding="utf-8"))["hypotheses"][0]["status"], "contradicted")
            passing_results = root / "passing.xml"
            write_junit(passing_results)
            fixed_manifest = self._reproduce(root, GOOD, "fixed.vcd", passing_results, 0)
            fixed = root / "fixed-verification.json"
            report = root / "fixed-report.md"
            result = invoke(
                "case", "verify-fix", "--case", str(before), "--waveform", "fixed.vcd",
                "--verification-manifest", str(fixed_manifest), "--outcome", "fixed", "--workspace", str(root), "--out", str(fixed), "--report", str(report),
            )
            self.assertEqual(result.returncode, 0)
            self.assertEqual(json.loads(fixed.read_text(encoding="utf-8"))["outcome"], "fixed")
            self.assertIn("Decision: `fixed`", report.read_text(encoding="utf-8"))
            not_fixed_results = root / "not-fixed.xml"
            write_junit(not_fixed_results)
            not_fixed_manifest = self._reproduce(root, BAD, "not-fixed.vcd", not_fixed_results, 0)
            outcome = root / "not-fixed-verification.json"
            result = invoke(
                "case", "verify-fix", "--case", str(before), "--waveform", "not-fixed.vcd",
                "--verification-manifest", str(not_fixed_manifest), "--outcome", "fixed", "--workspace", str(root), "--out", str(outcome), check=False,
            )
            self.assertEqual(result.returncode, 2)
            self.assertEqual(json.loads(outcome.read_text(encoding="utf-8"))["outcome"], "not-fixed")
            incomplete_manifest = root / "incomplete.manifest.json"
            payload = json.loads(fixed_manifest.read_text(encoding="utf-8"))
            payload["failure"]["relation"] = "unknown"
            incomplete_manifest.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
            incomplete = root / "incomplete-verification.json"
            result = invoke(
                "case", "verify-fix", "--case", str(before), "--waveform", "fixed.vcd",
                "--verification-manifest", str(incomplete_manifest), "--outcome", "fixed", "--workspace", str(root), "--out", str(incomplete), check=False,
            )
            self.assertEqual(result.returncode, 2)
            self.assertEqual(json.loads(incomplete.read_text(encoding="utf-8"))["outcome"], "verification-incomplete")

    def test_reproduce_multiple_waveforms_keeps_run_record(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            command = f"cp {GOOD} {root / 'one.vcd'}; cp {GOOD} {root / 'two.vcd'}"
            manifest = root / "run.json"
            result = invoke("reproduce", "--workspace", str(root), "--run-command", command, "--out", str(manifest), check=False)
            self.assertEqual(result.returncode, 2)
            record = json.loads(manifest.read_text(encoding="utf-8"))
            self.assertIn("multiple waveform candidates", record["waveform_selection_error"])


if __name__ == "__main__":
    unittest.main()
