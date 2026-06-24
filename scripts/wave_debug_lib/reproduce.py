"""Explicit simulation execution and framework-neutral JUnit run capture."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import re
import subprocess
from typing import Any
import xml.etree.ElementTree as ET

from .project import WAVE_SUFFIXES, discover_files


TIME_RE = re.compile(r"(?<![A-Za-z0-9_])(\d+(?:\.\d+)?)\s*(s|ms|us|ns|ps|fs)\b", re.I)


def waveform_snapshot(workspace: Path) -> dict[Path, tuple[int, int]]:
    return {path: (path.stat().st_size, path.stat().st_mtime_ns) for path in discover_files(workspace, WAVE_SUFFIXES)}


def changed_waveforms(workspace: Path, before: dict[Path, tuple[int, int]]) -> list[Path]:
    changed: list[Path] = []
    for path in discover_files(workspace, WAVE_SUFFIXES):
        identity = (path.stat().st_size, path.stat().st_mtime_ns)
        if before.get(path) != identity:
            changed.append(path)
    return changed


def parse_junit(path: Path) -> dict[str, object]:
    try:
        root = ET.parse(path).getroot()
    except (ET.ParseError, OSError) as error:
        raise ValueError(f"cannot parse JUnit results: {path}: {error}") from error
    cases: list[dict[str, object]] = []
    candidates: list[str] = []
    for node in root.iter("testcase"):
        failure_nodes = [item for item in node if item.tag.rsplit("}", 1)[-1] in {"failure", "error"}]
        messages = []
        for failure in failure_nodes:
            text = " ".join(filter(None, (failure.get("message"), failure.text))).strip()
            messages.append(text)
            candidates.extend(match.group(0).replace(" ", "") for match in TIME_RE.finditer(text))
        cases.append({
            "name": node.get("name"), "classname": node.get("classname"),
            "status": "failed" if failure_nodes else "passed", "failure_messages": messages,
        })
    return {
        "path": str(path.resolve()), "testcase_count": len(cases),
        "failure_count": sum(1 for item in cases if item["status"] == "failed"),
        "testcases": cases, "failure_time_candidates": list(dict.fromkeys(candidates)),
    }


def execute(command: str, workspace: Path, stdout_path: Path, stderr_path: Path) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(command, shell=True, cwd=workspace, text=True, capture_output=True, check=False)
    stdout_path.write_text(completed.stdout, encoding="utf-8")
    stderr_path.write_text(completed.stderr, encoding="utf-8")
    return completed


def run_record(
    command: str, workspace: Path, output: Path, before: dict[Path, tuple[int, int]], expected_waveform: Path | None,
    results: Path | None, testcase: str | None, failure_time: str | None, failure_label: str | None,
    confirm_failure: bool, confirm_passing: bool,
) -> tuple[dict[str, Any], int, Path | None]:
    output.mkdir(parents=True, exist_ok=True)
    completed = execute(command, workspace, output / "stdout.log", output / "stderr.log")
    junit = parse_junit(results) if results is not None and results.is_file() else None
    if expected_waveform is not None:
        selected = expected_waveform.resolve()
        selection_error = None
        if not selected.is_file() or selected.suffix.lower() not in WAVE_SUFFIXES:
            selection_error = f"expected waveform was not produced: {selected}"
            selected = None
        elif before.get(selected) == (selected.stat().st_size, selected.stat().st_mtime_ns):
            selection_error = f"expected waveform was not updated by run: {selected}"
    else:
        candidates = changed_waveforms(workspace, before)
        if len(candidates) == 1:
            selected = candidates[0]
            selection_error = None
        elif len(candidates) > 1:
            selection_error = "run produced multiple waveform candidates; pass --waveform: " + "; ".join(str(path) for path in candidates)
            selected = None
        else:
            selected = None
            selection_error = "run produced no new or updated waveform"
    failures = int(junit["failure_count"]) if junit is not None else 0
    if confirm_failure or completed.returncode != 0 or failures:
        relation = "confirmed-failure"
    elif confirm_passing or (junit is not None and failures == 0 and completed.returncode == 0):
        relation = "confirmed-passing"
    else:
        relation = "unknown"
    record: dict[str, Any] = {
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "run": {
            "command": command, "workspace": str(workspace.resolve()), "exit_code": completed.returncode,
            "stdout": str((output / "stdout.log").resolve()), "stderr": str((output / "stderr.log").resolve()),
            "testcase": testcase,
        },
        "junit": junit,
        "failure": {"time": failure_time, "label": failure_label, "relation": relation},
        "waveform_path": str(selected) if selected is not None else None,
    }
    if selection_error is not None:
        record["waveform_selection_error"] = selection_error
    return record, completed.returncode, selected


def write_run_record(path: Path, record: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8")
