"""Small, reviewable Markdown reports derived from bounded probe evidence."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def write_probe_report(
    path: Path, evidence: dict[str, Any], inferences: list[str], hypotheses: list[str], command: str,
) -> None:
    waveform = evidence["waveform"]
    lines = [
        "# Waveform evidence report", "", "## Provenance", "",
        f"- Waveform: `{waveform['path']}`",
        f"- Format/backend: `{waveform['format']}` / `{waveform['backend']}`",
        f"- Window: `{evidence['window']['start']['display']}` to `{evidence['window']['end']['display']}`",
        f"- Sampling: `{evidence['sampling']['phase']}` (offline event-region ordering is unavailable)",
        f"- Command: `{command}`", "", "## Observed", "",
        "| Time | Signal | Value |", "| --- | --- | --- |",
    ]
    changes = evidence.get("changes", [])
    for change in changes:
        lines.append(f"| {change['time']['display']} | `{change['signal']}` | `{change['value']}` |")
    if not changes:
        lines.append("| — | No selected signal changes in the window | — |")
    lines.extend(["", "## Inferred", ""])
    lines.extend(f"- {item}" for item in inferences) if inferences else lines.append("- None supplied.")
    lines.extend(["", "## Hypothesis", ""])
    lines.extend(f"- {item}" for item in hypotheses) if hypotheses else lines.append("- None supplied.")
    lines.extend(["", "## Next probe", "", evidence.get("next_probe") or "Narrow to one causal hypothesis or extend the bounded window.", ""])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")


def write_case_report(path: Path, case: dict[str, Any], validation: dict[str, Any], command: str) -> None:
    waveform = case["provenance"]["waveform"]
    lines = [
        "# Waveform hypothesis validation report", "", "## Provenance", "",
        f"- Waveform: `{waveform['path']}`",
        f"- Case: `{case['case_id']}` revision `{case['revision']}`",
        f"- Sampling: `{validation['sampling_phase']}` (offline event-region ordering is unavailable)",
        f"- Command: `{command}`", "", "## Observed", "",
        f"- Symptom: {case['symptom']['summary'] or 'None supplied.'}",
    ]
    authority_tiers: set[str] = set()
    for result in validation["results"]:
        for evidence_path in result.get("evidence", []):
            packet_path = Path(str(evidence_path))
            if not packet_path.is_file():
                lines.append(f"- Evidence packet unavailable: `{packet_path}`")
                continue
            packet = json.loads(packet_path.read_text(encoding="utf-8"))
            for signal, series in packet.get("series", {}).items():
                events = series.get("events", [])
                last = events[-1] if events else None
                detail = f"initial `{series.get('before')}`; {len(events)} change(s)"
                if last is not None:
                    detail += f"; last tick `{last['ticks']}` = `{last['value_bits']}`"
                lines.append(f"- `{signal}`: {detail}.")
            authority_tiers.update(
                str(row.get("rtl", {}).get("mapping_confidence"))
                for row in packet.get("signals", [])
                if row.get("rtl", {}).get("mapping_confidence")
            )
    if authority_tiers:
        lines.append("- Authority tiers: " + ", ".join(f"`{item}`" for item in sorted(authority_tiers)) + ".")
    lines.extend(["", "## Validation", "", "| Hypothesis | Status | Check results |", "| --- | --- | --- |"])
    for result in validation["results"]:
        checks = "; ".join(f"{item['check_id']}: {item['status']}" for item in result["checks"])
        lines.append(f"| `{result['hypothesis_id']}` | `{result['status']}` | {checks or 'No checks'} |")
    lines.extend(["", "## Inferred", "", "- None supplied by the validator.", "", "## Hypothesis", ""])
    hypotheses = {item["id"]: item for item in case["hypotheses"]}
    for result in validation["results"]:
        hypothesis = hypotheses[result["hypothesis_id"]]
        lines.append(f"- `{hypothesis['id']}`: {hypothesis['description']}")
    lines.extend(["", "## Next probe", ""])
    next_steps = [str(result["next_probe"]) for result in validation["results"] if result.get("next_probe")]
    lines.extend(f"- {item}" for item in next_steps) if next_steps else lines.append("- No additional probe required by selected checks.")
    lines.append("")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")
