"""Immutable, evidence-backed validation of user-authored debug hypotheses."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
import re
from typing import Any, Iterable

from . import TOOL_VERSION
from .authority import lookup_authority
from .provenance import PROVENANCE_SCHEMA_VERSION
from .vcd import Signal, parse_time, normalize_value
from .wave import WaveBackend


CASE_SCHEMA_VERSION = "1.0"
CASE_STATUSES = {"active", "supported", "contradicted", "insufficient-evidence"}
CHECK_KINDS = {"value_at", "stable", "transition", "edge", "occurs_before"}
CONFIDENCE_LEVELS = {"low", "medium", "high", "unknown"}


def _json(value: object) -> str:
    return json.dumps(value, indent=2, sort_keys=True) + "\n"


def _slug(value: str) -> str:
    rendered = re.sub(r"[^A-Za-z0-9_.-]+", "-", value).strip(".-")
    return rendered or "case"


def build_case(
    case_id: str,
    provenance: dict[str, object],
    symptom: str | None,
    start: str | None,
    end: str | None,
    affected_signals: list[str],
    first_divergence: str | None,
) -> dict[str, object]:
    return {
        "schema_version": CASE_SCHEMA_VERSION,
        "case_id": _slug(case_id),
        "revision": 0,
        "provenance": provenance,
        "symptom": {
            "summary": symptom or "",
            "time_window": {"start": start, "end": end} if start is not None or end is not None else None,
            "affected_signals": sorted(set(affected_signals)),
            "first_divergence": first_divergence,
        },
        "hypotheses": [],
        "validation_history": [],
    }


def read_case(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise ValueError(f"invalid debug case JSON: {path}: {error.msg}") from error
    validate_case(payload)
    return payload


def write_case(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_json(payload), encoding="utf-8")


def validate_case(payload: object) -> None:
    if not isinstance(payload, dict) or payload.get("schema_version") != CASE_SCHEMA_VERSION:
        raise ValueError(f"unsupported debug case schema; expected {CASE_SCHEMA_VERSION}")
    required = ("case_id", "revision", "provenance", "symptom", "hypotheses", "validation_history")
    missing = [name for name in required if name not in payload]
    if missing:
        raise ValueError("debug case missing required fields: " + ", ".join(missing))
    if not isinstance(payload["case_id"], str) or not payload["case_id"]:
        raise ValueError("debug case case_id must be a non-empty string")
    if not isinstance(payload["revision"], int) or payload["revision"] < 0:
        raise ValueError("debug case revision must be a non-negative integer")
    provenance = payload["provenance"]
    if not isinstance(provenance, dict) or provenance.get("schema_version") != PROVENANCE_SCHEMA_VERSION:
        raise ValueError("debug case must embed a provenance manifest schema 1.0")
    waveform = provenance.get("waveform")
    if not isinstance(waveform, dict) or not isinstance(waveform.get("path"), str):
        raise ValueError("debug case provenance must contain waveform.path")
    if not isinstance(payload["hypotheses"], list) or not isinstance(payload["validation_history"], list):
        raise ValueError("debug case hypotheses and validation_history must be arrays")
    identifiers: set[str] = set()
    for hypothesis in payload["hypotheses"]:
        _validate_hypothesis(hypothesis, identifiers)


def _validate_hypothesis(hypothesis: object, identifiers: set[str]) -> None:
    if not isinstance(hypothesis, dict):
        raise ValueError("every hypothesis must be an object")
    for name in ("id", "description", "required_signals", "expected_checks", "falsification_checks", "confidence", "status"):
        if name not in hypothesis:
            raise ValueError(f"hypothesis missing {name}")
    identifier = hypothesis["id"]
    if not isinstance(identifier, str) or not identifier or identifier in identifiers:
        raise ValueError("hypothesis ids must be unique non-empty strings")
    identifiers.add(identifier)
    if hypothesis["status"] not in CASE_STATUSES:
        raise ValueError(f"hypothesis {identifier} has invalid status")
    if hypothesis["confidence"] not in CONFIDENCE_LEVELS:
        raise ValueError(f"hypothesis {identifier} has invalid confidence")
    signals = hypothesis["required_signals"]
    if not isinstance(signals, list) or not all(isinstance(item, str) and item for item in signals):
        raise ValueError(f"hypothesis {identifier} required_signals must be exact non-empty paths")
    check_ids: set[str] = set()
    for group in ("expected_checks", "falsification_checks"):
        checks = hypothesis[group]
        if not isinstance(checks, list):
            raise ValueError(f"hypothesis {identifier} {group} must be an array")
        for check in checks:
            _validate_check(check, identifier)
            check_id = str(check["id"])
            if check_id in check_ids:
                raise ValueError(f"hypothesis {identifier} check ids must be unique")
            check_ids.add(check_id)


def _validate_check(check: object, hypothesis_id: str) -> None:
    if not isinstance(check, dict) or check.get("kind") not in CHECK_KINDS:
        raise ValueError(f"hypothesis {hypothesis_id} contains an unsupported check")
    if not isinstance(check.get("id"), str) or not check["id"]:
        raise ValueError(f"hypothesis {hypothesis_id} checks need non-empty ids")
    kind = str(check["kind"])
    if kind == "value_at":
        _require(check, "signal", "time", "equals")
    elif kind == "stable":
        _require(check, "signal", "start", "end")
    elif kind == "transition":
        _require(check, "signal", "start", "end", "from", "to")
    elif kind == "edge":
        _require(check, "signal", "start", "end", "edge")
        if check["edge"] not in {"rising", "falling", "both"}:
            raise ValueError(f"check {check['id']} has invalid edge")
    else:
        _require(check, "first", "second", "start", "end")
        _validate_event(check["first"], str(check["id"]))
        _validate_event(check["second"], str(check["id"]))


def _validate_event(event: object, check_id: str) -> None:
    if not isinstance(event, dict) or event.get("kind") not in {"value", "edge"}:
        raise ValueError(f"occurs_before check {check_id} events must be value or edge predicates")
    _require(event, "signal")
    if event["kind"] == "value":
        _require(event, "equals")
    elif event.get("edge") not in {"rising", "falling", "both"}:
        raise ValueError(f"occurs_before check {check_id} has invalid edge event")


def _require(payload: dict[str, object], *names: str) -> None:
    missing = [name for name in names if name not in payload]
    if missing:
        raise ValueError("check missing fields: " + ", ".join(missing))


def ensure_case_waveform(case: dict[str, Any]) -> Path:
    waveform = case["provenance"]["waveform"]
    path = Path(str(waveform["path"])).resolve()
    if not path.is_file():
        raise ValueError(f"case waveform no longer exists: {path}")
    stat = path.stat()
    if stat.st_size != waveform.get("size_bytes") or stat.st_mtime_ns != waveform.get("mtime_ns"):
        raise ValueError("case provenance mismatch: waveform size or modification time changed")
    return path


def validate_case_hypotheses(
    case: dict[str, Any], wave: WaveBackend, authority_db: Path | None, max_changes: int,
    selected_ids: set[str] | None = None,
) -> tuple[dict[str, Any], dict[str, dict[str, object]]]:
    """Return a new case revision and bounded evidence packets keyed by check id."""
    if max_changes < 1:
        raise ValueError("--max-changes must be >= 1")
    signals = {signal.path: signal for signal in wave.header.signals}
    selected = [item for item in case["hypotheses"] if selected_ids is None or item["id"] in selected_ids]
    if selected_ids is not None:
        missing = sorted(selected_ids - {item["id"] for item in selected})
        if missing:
            raise ValueError("unknown hypothesis id: " + ", ".join(missing))
    revision = deepcopy(case)
    revision["revision"] = int(case["revision"]) + 1
    evidence: dict[str, dict[str, object]] = {}
    results: list[dict[str, object]] = []
    by_id = {item["id"]: item for item in revision["hypotheses"]}
    for original in selected:
        hypothesis = by_id[original["id"]]
        outcome, check_results, packets = _validate_hypothesis_runtime(
            hypothesis, wave, signals, authority_db, max_changes,
        )
        hypothesis["status"] = outcome
        for check_id, packet in packets.items():
            evidence[f"{hypothesis['id']}__{check_id}"] = packet
        results.append({
            "hypothesis_id": hypothesis["id"], "status": outcome, "checks": check_results,
            "next_probe": _next_probe(check_results),
        })
    revision["validation_history"].append({
        "revision": revision["revision"],
        "validated_at": datetime.now(timezone.utc).isoformat(),
        "tool_version": TOOL_VERSION,
        "sampling_phase": "waveform-observed",
        "results": results,
    })
    return revision, evidence


def _validate_hypothesis_runtime(
    hypothesis: dict[str, Any], wave: WaveBackend, signals: dict[str, Signal], authority_db: Path | None,
    max_changes: int,
) -> tuple[str, list[dict[str, object]], dict[str, dict[str, object]]]:
    check_results: list[dict[str, object]] = []
    packets: dict[str, dict[str, object]] = {}
    missing_required = [path for path in hypothesis["required_signals"] if path not in signals]
    if missing_required:
        return "insufficient-evidence", [{
            "check_id": "required_signals", "kind": "required_signals", "role": "expected",
            "status": "insufficient-evidence", "detail": f"exact signal path not found: {missing_required[0]}",
            "start_ticks": None, "end_ticks": None,
        }], packets
    for role, checks in (("expected", hypothesis["expected_checks"]), ("falsification", hypothesis["falsification_checks"])):
        for check in checks:
            result, packet = evaluate_check(check, wave, signals, authority_db, max_changes)
            result["role"] = role
            check_results.append(result)
            packets[str(check["id"])] = packet
    falsified = any(item["role"] == "falsification" and item["status"] == "passed" for item in check_results)
    insufficient = any(item["status"] == "insufficient-evidence" for item in check_results)
    expected_passed = all(item["status"] == "passed" for item in check_results if item["role"] == "expected")
    expected_failed = any(item["role"] == "expected" and item["status"] == "failed" for item in check_results)
    if falsified:
        return "contradicted", check_results, packets
    if insufficient or expected_failed:
        return "insufficient-evidence", check_results, packets
    if expected_passed:
        return "supported", check_results, packets
    return "insufficient-evidence", check_results, packets


def evaluate_check(
    check: dict[str, Any], wave: WaveBackend, signals: dict[str, Signal], authority_db: Path | None,
    max_changes: int,
) -> tuple[dict[str, object], dict[str, object]]:
    try:
        start, end = _check_window(check, wave)
        referenced = _check_signals(check)
        selected = [_exact_signal(signals, path) for path in referenced]
        series, truncated = _collect_series(wave, selected, start, end, max_changes)
        if truncated:
            return _result(check, "insufficient-evidence", "selected event stream exceeded --max-changes", start, end), _packet(
                check, selected, series, start, end, truncated, authority_db,
            )
        status, detail = _evaluate(check, selected, series, start, end)
        return _result(check, status, detail, start, end), _packet(check, selected, series, start, end, False, authority_db)
    except (ValueError, KeyError) as error:
        return _result(check, "insufficient-evidence", str(error), None, None), {
            "schema_version": CASE_SCHEMA_VERSION, "check": check, "error": str(error),
            "sampling_phase": "waveform-observed",
        }


def _check_window(check: dict[str, Any], wave: WaveBackend) -> tuple[int, int]:
    if check["kind"] == "value_at":
        point = parse_time(check["time"], wave.header.timescale)
        return point, point
    start = parse_time(check["start"], wave.header.timescale)
    end = parse_time(check["end"], wave.header.timescale)
    if end < start:
        raise ValueError("check window end must not precede start")
    return start, end


def _check_signals(check: dict[str, Any]) -> list[str]:
    if check["kind"] == "occurs_before":
        return [str(check["first"]["signal"]), str(check["second"]["signal"])]
    return [str(check["signal"])]


def _exact_signal(signals: dict[str, Signal], path: str) -> Signal:
    if path not in signals:
        raise ValueError(f"case requires exact elaborated signal path not found: {path}")
    return signals[path]


def _collect_series(
    wave: WaveBackend, selected: list[Signal], start: int, end: int, max_changes: int,
) -> tuple[dict[str, dict[str, object]], bool]:
    by_id: dict[str, list[Signal]] = {}
    for signal in selected:
        by_id.setdefault(signal.id_code, []).append(signal)
    series: dict[str, dict[str, object]] = {signal.path: {"before": None, "events": []} for signal in selected}
    total = 0
    for timestamp, id_code, value in wave.changes(selected):
        for signal in by_id.get(id_code, []):
            bits = normalize_value(value, signal.width)
            row = series[signal.path]
            if timestamp < start:
                row["before"] = bits
            elif timestamp <= end:
                total += 1
                if total > max_changes:
                    return series, True
                row["events"].append({"ticks": timestamp, "value_bits": bits})
    for row in series.values():
        row["events"].sort(key=lambda item: int(item["ticks"]))
    return series, False


def _evaluate(
    check: dict[str, Any], selected: list[Signal], series: dict[str, dict[str, object]], start: int, end: int,
) -> tuple[str, str]:
    kind = check["kind"]
    if kind == "value_at":
        signal = selected[0]
        value = _value_at(series[signal.path], start)
        if value is None:
            return "insufficient-evidence", "no initialized value at requested time"
        return ("passed", "value matched") if value == _expected(check["equals"], signal.width) else ("failed", f"observed {value}")
    if kind == "stable":
        signal = selected[0]
        row = series[signal.path]
        initial = _value_at(row, start)
        if initial is None:
            return "insufficient-evidence", "no initialized value at window start"
        values = [initial] + [str(item["value_bits"]) for item in row["events"]]
        if "equals" in check:
            expected = _expected(check["equals"], signal.width)
            return ("passed", "value remained expected") if all(value == expected for value in values) else ("failed", "value changed or differed from expected")
        return ("passed", "no changes in window") if not row["events"] else ("failed", "signal changed in window")
    if kind == "transition":
        signal = selected[0]
        row = series[signal.path]
        initial = _value_at(row, start)
        if initial is None:
            return "insufficient-evidence", "no initialized value at window start"
        if initial != _expected(check["from"], signal.width):
            return "failed", f"window starts at {initial}, not required from value"
        target = _expected(check["to"], signal.width)
        return ("passed", "transition observed") if any(item["value_bits"] == target for item in row["events"]) else ("failed", "required transition not observed")
    if kind == "edge":
        signal = selected[0]
        timestamp = _event_time({"kind": "edge", "signal": signal.path, "edge": check["edge"]}, signal, series[signal.path])
        return ("passed", f"edge at tick {timestamp}") if timestamp is not None else ("failed", "required edge not observed")
    first_signal, second_signal = selected
    first_time = _event_time(check["first"], first_signal, series[first_signal.path])
    second_time = _event_time(check["second"], second_signal, series[second_signal.path])
    if first_time is None or second_time is None:
        return "failed", "one or both ordered events were not observed"
    return ("passed", "event order observed") if first_time < second_time else ("failed", "event order contradicted")


def _value_at(row: dict[str, object], timestamp: int) -> str | None:
    value = row["before"]
    for event in row["events"]:
        if int(event["ticks"]) > timestamp:
            break
        value = event["value_bits"]
    return str(value) if value is not None else None


def _expected(value: object, width: int) -> str:
    text = str(value).lower()
    if not text or not set(text) <= {"0", "1", "x", "z"}:
        raise ValueError("check values must be raw 0/1/x/z bit strings")
    return normalize_value(text, width)


def _event_time(event: dict[str, Any], signal: Signal, row: dict[str, object]) -> int | None:
    previous = row["before"]
    for item in row["events"]:
        current = str(item["value_bits"])
        if event["kind"] == "value":
            matched = current == _expected(event["equals"], signal.width)
        else:
            rising = previous == "0" and current == "1"
            falling = previous == "1" and current == "0"
            matched = previous is not None and (
                event["edge"] == "both" or (event["edge"] == "rising" and rising) or (event["edge"] == "falling" and falling)
            )
        if matched:
            return int(item["ticks"])
        previous = current
    return None


def _result(check: dict[str, Any], status: str, detail: str, start: int | None, end: int | None) -> dict[str, object]:
    return {"check_id": check.get("id"), "kind": check.get("kind"), "status": status, "detail": detail, "start_ticks": start, "end_ticks": end}


def _packet(
    check: dict[str, Any], selected: list[Signal], series: dict[str, dict[str, object]], start: int, end: int,
    truncated: bool, authority_db: Path | None,
) -> dict[str, object]:
    authority = lookup_authority(authority_db, [signal.path for signal in selected])
    return {
        "schema_version": CASE_SCHEMA_VERSION,
        "check": check,
        "query": {"signals": [signal.path for signal in selected], "start_ticks": start, "end_ticks": end, "sampling_phase": "waveform-observed"},
        "truncated": truncated,
        "signals": [
            {**signal.as_dict(), "rtl": authority.get(signal.path, {"match_status": "unresolved", "reason": "authority-not-provided"})}
            for signal in selected
        ],
        "series": series,
    }


def _next_probe(check_results: Iterable[dict[str, object]]) -> str | None:
    unresolved = next((item for item in check_results if item["status"] == "insufficient-evidence"), None)
    if unresolved is not None:
        return f"resolve check {unresolved['check_id']}: {unresolved['detail']}"
    failed = next((item for item in check_results if item["status"] == "failed"), None)
    if failed is not None:
        return f"inspect the smallest window around check {failed['check_id']}"
    return None
