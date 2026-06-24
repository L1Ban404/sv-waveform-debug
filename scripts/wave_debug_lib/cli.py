from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shutil
import sys
from typing import Any

from . import SCHEMA_VERSION, TOOL_VERSION
from .analysis import cache_key, compare_waveforms, infer_roles, probe, select_signals, signal_value, suggest_paths
from .authority import authority_diagnostics, authority_fingerprint, build_authority, lookup_authority
from .case import (
    build_case, ensure_case_waveform, read_case, validate_case_hypotheses, write_case,
)
from .project import SourceManifest, infer_top, module_candidates, resolve_waveform, source_manifest, waveform_candidates
from .provenance import build_provenance, read_provenance, write_provenance
from .reproduce import run_record, waveform_snapshot, write_run_record
from .report import write_case_report, write_fix_report, write_probe_report
from .vcd import parse_time
from .wave import open_waveform, pywellen_diagnostics


def skill_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _json(value: object) -> str:
    if isinstance(value, dict):
        value = dict(value)
        value.setdefault("tool_version", TOOL_VERSION)
        value.setdefault("contract_schema", value.get("schema_version"))
    return json.dumps(value, indent=2, sort_keys=True) + "\n"


def _add_wave_inputs(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    parser.add_argument("--waveform", type=Path)
    parser.add_argument("--out-dir", type=Path)


def _add_source_inputs(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--source-root", type=Path)
    parser.add_argument("--source", type=Path, action="append", default=[])
    parser.add_argument("--filelist", type=Path, action="append", default=[])
    parser.add_argument("--include", type=Path, action="append", default=[])
    parser.add_argument("--define", action="append", default=[])
    parser.add_argument("--exclude", action="append", default=[])
    parser.add_argument("--top")
    parser.add_argument("--parameter", action="append", default=[], help="parameter override recorded in provenance")
    parser.add_argument("--simulator", help="simulator name recorded in provenance")
    parser.add_argument("--simulator-version", help="simulator version recorded in provenance")
    parser.add_argument("--simulation-command", help="reproducible simulation command recorded in provenance")
    parser.add_argument("--failure-time", help="failure time recorded in provenance")
    parser.add_argument("--failure-label", help="assertion, testcase, or external failure label")
    parser.add_argument("--confirm-failure", action="store_true", help="explicitly confirm this waveform belongs to a failing run")
    parser.add_argument("--provenance-file", type=Path, help="read a previously generated provenance manifest")


def _add_match_inputs(parser: argparse.ArgumentParser, *, recursive: bool = True) -> None:
    parser.add_argument("--match", action="append", default=[], help="case-insensitive literal substring of the local signal name (repeat to AND)")
    parser.add_argument("--name-regex", action="append", default=[], help="case-insensitive regular expression of the local signal name")
    parser.add_argument("--path-match", action="append", default=[], help="case-insensitive literal substring of the full hierarchy path")
    parser.add_argument("--path-regex", action="append", default=[], help="case-insensitive regular expression of the full hierarchy path")
    parser.add_argument("--regex", action="append", default=[], help="deprecated alias for --path-regex")
    if recursive:
        parser.add_argument("--recursive", action=argparse.BooleanOptionalAction, default=True, help="include descendant scopes (default: recursive)")


def _paths(args: argparse.Namespace) -> tuple[Path, Path, Path, list[Path]]:
    workspace = args.workspace.resolve()
    waveform, candidates = resolve_waveform(workspace, args.waveform)
    assert waveform is not None
    output = args.out_dir or Path("build/wave-debug")
    output = output if output.is_absolute() else workspace / output
    return workspace, waveform, output, candidates


def _manifest(args: argparse.Namespace, workspace: Path) -> SourceManifest:
    _apply_provenance_inputs(args, workspace)
    return source_manifest(
        workspace,
        getattr(args, "source_root", None),
        getattr(args, "source", []),
        getattr(args, "filelist", []),
        getattr(args, "include", []),
        getattr(args, "define", []),
        getattr(args, "exclude", []),
    )


def _apply_provenance_inputs(args: argparse.Namespace, workspace: Path) -> None:
    """Fill omitted compilation context from an explicit portable manifest."""
    path = getattr(args, "provenance_file", None)
    if not path or getattr(args, "_provenance_applied", False):
        return
    resolved = path if path.is_absolute() else workspace / path
    payload = read_provenance(resolved)
    compilation = payload.get("compilation", {})
    simulation = payload.get("simulation", {})
    failure = payload.get("failure", {})
    if not args.source and not args.filelist:
        args.source = [Path(item) for item in compilation.get("source_files", [])]
    if not args.include:
        args.include = [Path(item) for item in compilation.get("include_dirs", [])]
    if not args.define:
        args.define = list(compilation.get("defines", []))
    if not args.parameter:
        args.parameter = list(compilation.get("parameter_overrides", []))
    if not args.top:
        args.top = compilation.get("top")
    if not args.simulator:
        args.simulator = simulation.get("simulator")
    if not args.simulator_version:
        args.simulator_version = simulation.get("simulator_version")
    if not args.simulation_command:
        args.simulation_command = simulation.get("command")
    if not args.failure_time:
        args.failure_time = failure.get("time")
    if not args.failure_label:
        args.failure_label = failure.get("label")
    args.provenance_file = resolved
    args._provenance_applied = True


def _top(args: argparse.Namespace, manifest: SourceManifest, wave: Any, waveform: Path, required: bool) -> tuple[str | None, list[str]]:
    candidates = module_candidates(manifest.files)
    root_scopes = {scope.local_name for scope in wave.header.scopes if scope.parent is None}
    selected = args.top or infer_top(candidates, root_scopes, waveform)
    if required and not selected:
        rendered = ", ".join(candidates) if candidates else "<none>"
        raise ValueError(f"top module is ambiguous; pass --top (candidates: {rendered})")
    return selected, candidates


def _top_selection(args: argparse.Namespace, candidates: list[str], selected: str | None) -> str:
    if args.top:
        return "explicit --top"
    if selected is None:
        return "none: pass --top when source/elaboration needs a top"
    if len(candidates) == 1:
        return "only discovered source top candidate"
    return "unique discovered source top candidate matching a waveform root scope"


def _authority_db(args: argparse.Namespace, workspace: Path, output: Path, top: str | None) -> Path | None:
    explicit = getattr(args, "authority_db", None)
    if explicit:
        return explicit if explicit.is_absolute() else workspace / explicit
    if top:
        candidate = output / "authority" / top / "rtl_authority.sqlite3"
        if candidate.is_file():
            return candidate
    return None


def _run_provenance(args: argparse.Namespace, wave: Any, manifest: SourceManifest, top: str | None) -> dict[str, object]:
    relation = "confirmed-failure" if getattr(args, "confirm_failure", False) else "unknown"
    current = build_provenance(
        wave, manifest, top,
        simulator=getattr(args, "simulator", None),
        simulator_version=getattr(args, "simulator_version", None),
        simulation_command=getattr(args, "simulation_command", None),
        parameter_overrides=getattr(args, "parameter", []),
        failure_time=getattr(args, "failure_time", None),
        failure_label=getattr(args, "failure_label", None),
        failure_relation=relation,
    )
    supplied_path = getattr(args, "provenance_file", None)
    if not supplied_path:
        return {"current": current, "provided": None}
    supplied = read_provenance(supplied_path)
    supplied_wave = str(supplied["waveform"].get("path", ""))
    if not getattr(args, "confirm_failure", False):
        current["failure"]["relation"] = supplied["failure"].get("relation", "unknown")
        if current["failure"]["time"] is None:
            current["failure"]["time"] = supplied["failure"].get("time")
        if current["failure"]["label"] is None:
            current["failure"]["label"] = supplied["failure"].get("label")
    return {
        "current": current,
        "provided": {
            "path": str(supplied_path.resolve()),
            "waveform_matches_current": supplied_wave == current["waveform"]["path"],
            "record": supplied,
        },
    }


def _doctor(as_json: bool) -> int:
    root = skill_root()
    pywellen = pywellen_diagnostics(root)
    result = {
        "schema_version": SCHEMA_VERSION,
        "version_registry": {
            "tool": TOOL_VERSION, "waveform_evidence": SCHEMA_VERSION,
            "authority": SCHEMA_VERSION, "provenance": "1.0", "debug_case": "1.0",
        },
        "python": {"version": sys.version.split()[0], "supported_vcd": sys.version_info >= (3, 10)},
        "capabilities": {
            "vcd": {"available": True, "backend": "python-vcd"},
            "fst_direct": pywellen,
            "fst_conversion": {"available": shutil.which("fst2vcd") is not None, "path": shutil.which("fst2vcd")},
            "rtl_authority": authority_diagnostics(),
        },
        "ready": sys.version_info >= (3, 10),
        "remediation": [],
    }
    if not pywellen["available"] and shutil.which("fst2vcd") is None:
        result["remediation"].append("install a compatible pywellen or fst2vcd to read FST waveforms")
    if as_json:
        print(_json(result), end="")
    else:
        print(f"Python: {result['python']['version']} (portable VCD: yes)")
        for name, capability in result["capabilities"].items():
            print(f"{name}: {'available' if capability['available'] else 'unavailable'}")
        for item in result["remediation"]:
            print(f"remediation: {item}")
    return 0


def _inspect(args: argparse.Namespace) -> int:
    workspace = args.workspace.resolve()
    waveform, candidates = resolve_waveform(workspace, args.waveform, allow_ambiguous=True)
    output = args.out_dir or Path("build/wave-debug")
    output = output if output.is_absolute() else workspace / output
    candidate_rows = waveform_candidates(candidates)
    if waveform is None:
        result = {
            "schema_version": SCHEMA_VERSION,
            "workspace": str(workspace),
            "waveform": {
                "selected": None,
                "selection": "explicit --waveform required: multiple candidates found",
                "candidates": candidate_rows if args.verbose else candidate_rows[:10],
            },
            "output_dir": str(output),
            "next_command": "rerun the failed test, then pass its new waveform with --waveform PATH",
        }
        if args.json:
            print(_json(result), end="")
        else:
            print("waveform: <none; explicit selection required>")
            for row in candidate_rows:
                print(f"candidate: {row['path']}  modified={row['modified_at']}  size={row['size_bytes']}")
            print(f"next: {result['next_command']}")
        return 0
    wave = open_waveform(waveform, skill_root(), output)
    manifest = _manifest(args, workspace)
    selected_top, top_candidates = _top(args, manifest, wave, waveform, False)
    run_provenance = _run_provenance(args, wave, manifest, selected_top)
    current_provenance = run_provenance["current"]
    assert isinstance(current_provenance, dict)
    result = {
        "schema_version": SCHEMA_VERSION,
        "workspace": str(workspace),
        "waveform": {
            "selected": str(waveform),
            "selection": "explicit --waveform" if args.waveform else "only waveform candidate",
            "format": wave.format,
            "backend": wave.backend,
            "candidates": candidate_rows if args.verbose else candidate_rows[:1],
        },
        "timescale": wave.header.timescale.as_dict(),
        "hierarchy": {"scope_count": len(wave.header.scopes), "signal_count": len(wave.header.signals)},
        "source": {
            "files": len(manifest.files),
        },
        "top_candidates": top_candidates,
        "selected_top": selected_top,
        "top_selection": _top_selection(args, top_candidates, selected_top),
        "role_candidates": infer_roles(wave.header.signals),
        "suggested_scope": selected_top or next((scope.path for scope in wave.header.scopes if scope.parent is None), None),
        "provenance": {
            "failure_relation": current_provenance["failure"]["relation"],
            "failure_time": current_provenance["failure"]["time"],
            "provided_manifest": run_provenance["provided"] and {
                "path": run_provenance["provided"]["path"],
                "waveform_matches_current": run_provenance["provided"]["waveform_matches_current"],
            },
        },
        "output_dir": str(output),
    }
    if args.verbose:
        result["source"].update({
            "filelists": [str(path) for path in manifest.filelists],
            "include_dirs": [str(path) for path in manifest.include_dirs],
            "defines": manifest.defines,
            "source_files": [str(path) for path in manifest.files],
        })
        result["provenance_detail"] = run_provenance
    if args.json:
        print(_json(result), end="")
    else:
        print(f"workspace: {workspace}")
        print(f"waveform: {waveform}")
        print(f"waveform-format: {wave.format}")
        print(f"waveform-backend: {wave.backend}")
        print(f"waveform-selection: {result['waveform']['selection']}")
        print(f"waveform-candidates: {len(candidates)}")
        print(f"timescale: {wave.header.timescale.factor}{wave.header.timescale.unit}")
        print(f"scopes: {len(wave.header.scopes)}")
        print(f"signals: {len(wave.header.signals)}")
        print(f"hdl-files: {len(manifest.files)}")
        print(f"top-candidates: {', '.join(top_candidates) if top_candidates else '<none>'}")
        print(f"selected-top: {selected_top or '<ambiguous>'}")
        print(f"top-selection: {result['top_selection']}")
        print(f"suggested-scope: {result['suggested_scope'] or '<none>'}")
        print(f"failure-relation: {result['provenance']['failure_relation']}")
        print(f"output-dir: {output}")
    return 0


def _scopes(args: argparse.Namespace) -> int:
    _workspace, waveform, output, _ = _paths(args)
    wave = open_waveform(waveform, skill_root(), output)
    scope = args.scope[4:] if args.scope and args.scope.startswith("TOP.") else args.scope

    def in_scope(item: Any) -> bool:
        if not scope:
            return True
        if args.recursive:
            return item.path == scope or item.path.startswith(scope + ".")
        return item.path == scope or item.parent == scope

    rows = [
        item.as_dict() for item in wave.header.scopes
        if in_scope(item)
        and (not args.match or all(term.lower() in item.local_name.lower() for term in args.match))
        and (not args.path_match or all(term.lower() in item.path.lower() for term in args.path_match))
    ]
    suggestions = suggest_paths(scope or (args.match[-1] if args.match else None), (item.path for item in wave.header.scopes)) if not rows else []
    if args.json:
        print(_json({"schema_version": SCHEMA_VERSION, "waveform": str(waveform), "scopes": rows, "suggestions": suggestions}), end="")
    else:
        for row in rows:
            print(f"{row['path']} ({row['kind']})")
        if suggestions:
            print("no exact hierarchy match; suggestions: " + ", ".join(suggestions))
    return 0


def _signals(args: argparse.Namespace) -> int:
    _workspace, waveform, output, _ = _paths(args)
    wave = open_waveform(waveform, skill_root(), output)
    selected, truncated = select_signals(
        wave.header.signals, scope=args.scope, matches=args.match, regexes=args.regex,
        name_regexes=args.name_regex, path_matches=args.path_match, path_regexes=args.path_regex,
        recursive=args.recursive, limit=args.limit,
    )
    result = {
        "schema_version": SCHEMA_VERSION,
        "waveform": str(waveform),
        "count": len(selected),
        "truncated": truncated,
        "matching": {
            "match": "case-insensitive local signal-name substring; repeated terms are ANDed",
            "name_regex": "case-insensitive local signal-name regular expression",
            "path_match": "case-insensitive full hierarchy-path substring",
            "path_regex": "case-insensitive full hierarchy-path regular expression; --regex is an alias",
            "recursive": args.recursive,
        },
        "suggestions": suggest_paths(args.scope or ((args.match or args.regex)[-1] if args.match or args.regex else None), (signal.path for signal in wave.header.signals)) if not selected else [],
        "signals": [signal.as_dict() for signal in selected],
    }
    if args.json:
        print(_json(result), end="")
    else:
        for signal in selected:
            print(f"{signal.path} width={signal.width} kind={signal.kind}")
        if truncated:
            print(f"... truncated at --limit {args.limit}")
        elif not selected and result["suggestions"]:
            print("no signals matched; hierarchy suggestions: " + ", ".join(result["suggestions"]))
    return 0


def _resolve_signal(wave: Any, path: str):
    normalized = path[4:] if path.startswith("TOP.") else path
    matches = [signal for signal in wave.header.signals if signal.path == path or signal.path == normalized or signal.path == f"TOP.{normalized}"]
    if len(matches) != 1:
        raise ValueError(f"signal path must resolve exactly once: {path} (matches={len(matches)})")
    return matches[0]


def _signal(args: argparse.Namespace) -> int:
    workspace, waveform, output, _ = _paths(args)
    wave = open_waveform(waveform, skill_root(), output)
    timestamp = parse_time(args.time, wave.header.timescale)
    result = signal_value(
        wave, _resolve_signal(wave, args.signal_path), timestamp,
        _authority_db(args, workspace, output, None), args.radix,
    )
    print(_json(result), end="")
    return 0


def _probe(args: argparse.Namespace, save_packet: bool = False) -> int:
    workspace, waveform, output, _ = _paths(args)
    wave = open_waveform(waveform, skill_root(), output)
    if args.around is not None or args.around_failure:
        around_value = args.around if args.around is not None else _failure_time_from_manifest(args, workspace)
        around = parse_time(around_value, wave.header.timescale)
        radius = parse_time(args.radius, wave.header.timescale)
        start, end = max(0, around - radius), around + radius
    else:
        start = parse_time(args.start, wave.header.timescale)
        end = parse_time(args.end, wave.header.timescale)
    explicit = list(args.signal_path)
    if args.clock and args.clock not in explicit:
        explicit.append(args.clock)
    selected, signal_truncated = select_signals(
        wave.header.signals, scope=args.scope, matches=args.match, regexes=args.regex,
        name_regexes=args.name_regex, path_matches=args.path_match, path_regexes=args.path_regex,
        recursive=args.recursive, paths=explicit, limit=args.max_signals,
    )
    if not selected:
        raise ValueError("probe selected no signals; use `signals` to discover paths")
    manifest = _manifest(args, workspace) if hasattr(args, "source") else SourceManifest([], [], [], [])
    top, top_candidates = _top(args, manifest, wave, waveform, False) if hasattr(args, "top") else (None, [])
    database = _authority_db(args, workspace, output, top)
    result = probe(
        wave, selected, start, end, args.max_changes, database, args.clock, args.edge,
        args.radix, args.sample_phase,
    )
    result["truncated"] = bool(result["truncated"] or signal_truncated)
    result["provenance"] = {
        "source_file_count": len(manifest.files),
        "source_files": [str(path) for path in manifest.files[:20]],
        "source_files_truncated": len(manifest.files) > 20,
        "include_dirs": [str(path) for path in manifest.include_dirs],
        "defines": manifest.defines,
        "top": top,
        "top_selection": _top_selection(args, top_candidates, top),
        "authority": authority_fingerprint(database),
        "run_manifest": _run_provenance(args, wave, manifest, top),
    }
    if args.report:
        report_path = args.report if args.report.is_absolute() else workspace / args.report
        write_probe_report(
            report_path, result, args.inference, args.hypothesis,
            " ".join([Path(sys.argv[0]).name, args.command]),
        )
        result["report"] = str(report_path)
    if save_packet:
        source_identity = []
        for path in manifest.files:
            source_identity.append(
                {"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
            )
        identity = cache_key(
            {
                "waveform": str(waveform.resolve()), "size": waveform.stat().st_size,
                "mtime_ns": waveform.stat().st_mtime_ns, "top": top, "scope": args.scope,
                "start": start, "end": end, "schema": SCHEMA_VERSION,
                "sources": source_identity, "authority": authority_fingerprint(database),
            }
        )
        path = output / "packets" / identity / f"packet_{start}_{end}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(_json(result), encoding="utf-8")
        print(path)
    elif args.format == "json":
        print(_json(result), end="")
    else:
        if args.view in {"changes", "both"}:
            for change in result["changes"]:
                print(f"{change['time']['display']:>10}  {change['signal']} = {change['value']}")
        if args.view in {"snapshots", "both"}:
            samples = result["clock_samples"]
            if not args.clock:
                raise ValueError("--view snapshots requires --clock")
            if args.view == "both" and samples and result["changes"]:
                print()
            if not samples:
                print(f"<clock snapshots {result['clock_samples_status']}>")
            for sample in samples:
                values = sample["values"]
                rendered = "  ".join(f"{path}={values.get(path, '?')}" for path in sorted(values))
                print(f"{sample['time']['display']:>10}  {rendered}")
        if result["truncated"]:
            print("... output truncated; narrow the query")
    return 0


def _authority(args: argparse.Namespace) -> int:
    workspace, waveform, output, _ = _paths(args)
    wave = open_waveform(waveform, skill_root(), output)
    manifest = _manifest(args, workspace)
    if not manifest.files:
        raise ValueError("authority requires Verilog/SystemVerilog source")
    top, _ = _top(args, manifest, wave, waveform, True)
    assert top is not None
    destination = output / "authority" / top
    backend = build_authority(
        manifest.files, top, destination, args.force, args.authority_backend,
        manifest.include_dirs, manifest.defines, args.parameter,
    )
    authority = lookup_authority(destination / "rtl_authority.sqlite3", [signal.path for signal in wave.header.signals])
    mappings = []
    for signal in sorted(wave.header.signals, key=lambda item: item.path):
        row = authority.get(signal.path)
        if row is None:
            continue
        context = row.get("source_context") or []
        first = context[0] if context else {}
        mappings.append({
            "waveform_path": signal.path, "source_file": row.get("source_file"),
            "source_line": first.get("line"), "authority_tier": row.get("mapping_confidence"),
        })
        if len(mappings) >= args.summary_limit:
            break
    summary = {
        "schema_version": SCHEMA_VERSION, "destination": str(destination), "top": top,
        "backend": backend, "authority": authority_fingerprint(destination / "rtl_authority.sqlite3"),
        "mapping_count": len({signal.path for signal in wave.header.signals if signal.path in authority}), "mappings": mappings,
    }
    if args.json:
        print(_json(summary), end="")
    else:
        print(f"authority: {destination}")
        print(f"top: {top}  backend: {backend}  mappings: {summary['mapping_count']}")
        for row in mappings:
            source = f"{row['source_file']}:{row['source_line']}" if row["source_line"] else str(row["source_file"] or "<unresolved>")
            print(f"{row['waveform_path']} -> {source} [{row['authority_tier']}]")
    return 0


def _provenance(args: argparse.Namespace) -> int:
    workspace, waveform, output, _ = _paths(args)
    wave = open_waveform(waveform, skill_root(), output)
    manifest = _manifest(args, workspace)
    top, _ = _top(args, manifest, wave, waveform, False)
    payload = _run_provenance(args, wave, manifest, top)["current"]
    assert isinstance(payload, dict)
    destination = args.out or output / "provenance.json"
    destination = destination if destination.is_absolute() else workspace / destination
    write_provenance(destination, payload)
    print(destination)
    return 0


def _case_init(args: argparse.Namespace) -> int:
    if args.waveform is None:
        raise ValueError("case init requires an explicit --waveform")
    workspace, waveform, output, _ = _paths(args)
    wave = open_waveform(waveform, skill_root(), output)
    if (args.symptom_start is None) != (args.symptom_end is None):
        raise ValueError("--symptom-start and --symptom-end must be supplied together")
    if args.symptom_start is not None:
        start = parse_time(args.symptom_start, wave.header.timescale)
        end = parse_time(args.symptom_end, wave.header.timescale)
        if end < start:
            raise ValueError("symptom window end must not precede start")
    missing = [path for path in args.affected_signal if path not in {signal.path for signal in wave.header.signals}]
    if missing:
        raise ValueError(f"affected signals must be exact waveform paths: {missing[0]}")
    manifest = _manifest(args, workspace)
    top, _ = _top(args, manifest, wave, waveform, False)
    run_provenance = _run_provenance(args, wave, manifest, top)
    provided = run_provenance["provided"]
    if provided is not None and not provided["waveform_matches_current"]:
        raise ValueError("provided provenance manifest does not match --waveform")
    current_provenance = run_provenance["current"]
    assert isinstance(current_provenance, dict)
    if current_provenance["failure"]["relation"] != "confirmed-failure":
        raise ValueError(
            "case init requires a confirmed failing waveform; pass --confirm-failure or use a confirmed provenance manifest"
        )
    case = build_case(
        args.case_id or waveform.stem, run_provenance["current"], args.symptom,
        args.symptom_start, args.symptom_end, args.affected_signal, args.first_divergence,
    )
    destination = args.out or output / "cases" / str(case["case_id"]) / "case.json"
    destination = destination if destination.is_absolute() else workspace / destination
    if destination.exists() and not args.force:
        raise ValueError(f"case already exists: {destination}; pass --force to replace it")
    write_case(destination, case)
    print(destination)
    return 0


def _failure_time_from_manifest(args: argparse.Namespace, workspace: Path) -> str:
    path = getattr(args, "provenance_file", None)
    if path is None:
        raise ValueError("--around-failure requires --provenance-file with a confirmed failure")
    path = path if path.is_absolute() else workspace / path
    payload = read_provenance(path)
    failure = payload["failure"]
    if failure.get("relation") != "confirmed-failure":
        raise ValueError("--around-failure requires provenance relation confirmed-failure")
    value = failure.get("time")
    if not isinstance(value, str) or not value:
        raise ValueError("confirmed failure has no single failure time; pass --around or --failure-time")
    return value


def _reproduce(args: argparse.Namespace) -> int:
    workspace = args.workspace.resolve()
    output_root = args.out_dir or workspace / "build/wave-debug"
    output_root = output_root if output_root.is_absolute() else workspace / output_root
    before = waveform_snapshot(workspace)
    run_id = args.run_id or datetime.now(timezone.utc).strftime("run-%Y%m%dT%H%M%SZ")
    run_dir = output_root / "runs" / run_id
    expected = None
    if args.waveform is not None:
        expected = args.waveform if args.waveform.is_absolute() else workspace / args.waveform
    results = None
    if args.results is not None:
        results = args.results if args.results.is_absolute() else workspace / args.results
    record, exit_code, waveform = run_record(
        args.run_command, workspace, run_dir, before, expected, results, args.testcase,
        args.failure_time, args.failure_label, args.confirm_failure, args.confirm_passing,
    )
    manifest_path = args.out or run_dir / "manifest.json"
    manifest_path = manifest_path if manifest_path.is_absolute() else workspace / manifest_path
    if waveform is not None:
        wave = open_waveform(waveform, skill_root(), output_root)
        manifest = _manifest(args, workspace)
        top, _ = _top(args, manifest, wave, waveform, False)
        provenance = build_provenance(
            wave, manifest, top, simulator=args.simulator, simulator_version=args.simulator_version,
            simulation_command=args.run_command, parameter_overrides=args.parameter,
            failure_time=record["failure"]["time"], failure_label=args.failure_label,
            failure_relation=record["failure"]["relation"],
        )
        provenance["run"] = record["run"]
        provenance["junit"] = record["junit"]
        provenance["failure_time_candidates"] = record.get("junit", {}).get("failure_time_candidates", []) if record.get("junit") else []
        write_provenance(manifest_path, provenance)
    else:
        record["schema_version"] = "run-1.0"
        record["tool_version"] = TOOL_VERSION
        write_run_record(manifest_path, record)
    print(manifest_path)
    return exit_code if exit_code else (0 if waveform is not None else 2)


def _provenance_fingerprint(provenance: dict[str, object]) -> str:
    return hashlib.sha256(json.dumps(provenance, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def _case_validate(args: argparse.Namespace) -> int:
    workspace = args.workspace.resolve()
    case_path = args.case if args.case.is_absolute() else workspace / args.case
    case_path = case_path.resolve()
    case_root = case_path.parent.parent if case_path.parent.name == "revisions" else case_path.parent
    case = read_case(case_path)
    waveform_path = ensure_case_waveform(case)
    output = args.out_dir or case_path.parent / "build"
    output = output if output.is_absolute() else workspace / output
    wave = open_waveform(waveform_path, skill_root(), output)
    if not case["hypotheses"]:
        raise ValueError("case has no hypotheses; add one before validation")
    selected_ids = set(args.hypothesis) if args.hypothesis else None
    authority_db = args.authority_db
    if authority_db is not None and not authority_db.is_absolute():
        authority_db = workspace / authority_db
    revision, packets = validate_case_hypotheses(case, wave, authority_db, args.max_changes, selected_ids)
    validation = revision["validation_history"][-1]
    validation["command"] = "wave_debug.py case validate"
    validation["input_case"] = str(case_path)
    validation["provenance_fingerprint"] = _provenance_fingerprint(case["provenance"])
    destination = args.out
    if destination is None:
        destination = case_root / "revisions" / f"revision_{revision['revision']:03d}.json"
    destination = destination if destination.is_absolute() else workspace / destination
    if destination.exists():
        raise ValueError(f"validation snapshot already exists: {destination}")
    evidence_dir = case_root / "evidence" / f"revision_{revision['revision']:03d}"
    paths: dict[str, str] = {}
    for key, packet in packets.items():
        packet["provenance_fingerprint"] = validation["provenance_fingerprint"]
        packet["authority"] = authority_fingerprint(authority_db)
        evidence_path = evidence_dir / f"{key}.json"
        evidence_path.parent.mkdir(parents=True, exist_ok=True)
        evidence_path.write_text(_json(packet), encoding="utf-8")
        paths[key] = str(evidence_path)
    for result in validation["results"]:
        result["evidence"] = [
            paths[f"{result['hypothesis_id']}__{check['check_id']}"]
            for check in result["checks"]
            if f"{result['hypothesis_id']}__{check['check_id']}" in paths
        ]
    write_case(destination, revision)
    if args.report:
        report = args.report if args.report.is_absolute() else workspace / args.report
        write_case_report(report, revision, validation, validation["command"])
        validation["report"] = str(report)
        write_case(destination, revision)
    print(destination)
    return 0


def _case_verify_fix(args: argparse.Namespace) -> int:
    workspace = args.workspace.resolve()
    case_path = args.case if args.case.is_absolute() else workspace / args.case
    case_path = case_path.resolve()
    case_root = case_path.parent.parent if case_path.parent.name == "revisions" else case_path.parent
    case = read_case(case_path)
    if case["provenance"].get("failure", {}).get("relation") != "confirmed-failure":
        raise ValueError("case verify-fix requires a case built from a confirmed failing waveform")
    failing_waveform = ensure_case_waveform(case)
    verification_path = args.verification_manifest if args.verification_manifest.is_absolute() else workspace / args.verification_manifest
    manifest = read_provenance(verification_path.resolve())
    fixed = args.waveform if args.waveform.is_absolute() else workspace / args.waveform
    if not fixed.is_file():
        raise ValueError(f"fixed waveform does not exist: {fixed}")
    if Path(str(manifest["waveform"]["path"])).resolve() != fixed.resolve():
        raise ValueError("verification manifest waveform does not match --waveform")
    output = args.out_dir or case_root / "build"
    output = output if output.is_absolute() else workspace / output
    wave = open_waveform(fixed.resolve(), skill_root(), output)
    authority_db = args.authority_db
    if authority_db is not None and not authority_db.is_absolute():
        authority_db = workspace / authority_db
    after_case, packets = validate_case_hypotheses(case, wave, authority_db, args.max_changes)
    after = after_case["validation_history"][-1]["results"]
    run = manifest.get("run", {})
    junit = manifest.get("junit")
    confirmed_passing = manifest["failure"].get("relation") == "confirmed-passing"
    junit_clean = junit is not None and int(junit.get("failure_count", 0)) == 0
    checks_supported = bool(after) and all(item["status"] == "supported" for item in after)
    if not confirmed_passing or not junit_clean:
        outcome = "verification-incomplete"
    elif checks_supported:
        outcome = "fixed"
    else:
        outcome = "not-fixed"
    before = case["validation_history"][-1]["results"] if case["validation_history"] else []
    verification = {
        "schema_version": "fix-verification-1.0", "tool_version": TOOL_VERSION,
        "outcome_requested": args.outcome, "outcome": outcome,
        "failing_waveform": str(failing_waveform), "fixed_waveform": str(fixed.resolve()),
        "case": str(case_path), "verification_manifest": str(verification_path.resolve()),
        "verification_run": run, "junit": junit, "before": before, "after": after,
    }
    destination = args.out or case_root / "fix-verifications" / "verification.json"
    destination = destination if destination.is_absolute() else workspace / destination
    if destination.exists():
        raise ValueError(f"fix verification already exists: {destination}")
    evidence_dir = destination.parent / "evidence"
    paths: dict[str, str] = {}
    for key, packet in packets.items():
        packet["verification_manifest"] = str(verification_path.resolve())
        packet_path = evidence_dir / f"{key}.json"
        packet_path.parent.mkdir(parents=True, exist_ok=True)
        packet_path.write_text(_json(packet), encoding="utf-8")
        paths[key] = str(packet_path)
    verification["evidence"] = paths
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(_json(verification), encoding="utf-8")
    if args.report:
        report = args.report if args.report.is_absolute() else workspace / args.report
        write_fix_report(report, verification)
    print(destination)
    return 0 if outcome == "fixed" else 2


def _compare(args: argparse.Namespace) -> int:
    workspace = args.workspace.resolve()
    output = args.out_dir or workspace / "build/wave-debug"
    output = output if output.is_absolute() else workspace / output
    good_path = args.good if args.good.is_absolute() else workspace / args.good
    bad_path = args.bad if args.bad.is_absolute() else workspace / args.bad
    good = open_waveform(good_path.resolve(), skill_root(), output)
    bad = open_waveform(bad_path.resolve(), skill_root(), output)
    result = compare_waveforms(
        good, bad, args.scope, args.match, args.limit, args.regex,
        args.align, args.align_signal, args.align_occurrence,
    )
    print(_json(result), end="")
    return 0


def _probe_parser(sub: Any, name: str, help_text: str, window_required: bool = True) -> argparse.ArgumentParser:
    parser = sub.add_parser(name, help=help_text)
    _add_wave_inputs(parser)
    _add_source_inputs(parser)
    parser.add_argument("--authority-db", type=Path)
    window = parser.add_mutually_exclusive_group(required=window_required)
    window.add_argument("--around")
    window.add_argument("--around-failure", action="store_true")
    window.add_argument("--start")
    parser.add_argument("--end")
    parser.add_argument("--radius", default="100")
    parser.add_argument("--scope", "--focus-scope", dest="scope")
    _add_match_inputs(parser)
    parser.add_argument("--signal", dest="signal_path", action="append", default=[])
    parser.add_argument("--clock")
    parser.add_argument("--edge", choices=("rising", "falling", "both"), default="rising")
    parser.add_argument("--max-signals", type=int, default=64)
    parser.add_argument("--max-changes", type=int, default=200)
    parser.add_argument("--format", choices=("json", "table"), default="json")
    parser.add_argument("--radix", choices=("auto", "hex", "bin", "dec", "signed"), default="auto")
    parser.add_argument(
        "--sample-phase", choices=("waveform-observed", "pre-edge", "post-active", "post-nba", "postponed"),
        default="waveform-observed",
        help="snapshot phase; offline VCD/FST currently supports waveform-observed only",
    )
    parser.add_argument("--report", type=Path, help="write a reviewable Markdown evidence report")
    parser.add_argument("--inference", action="append", default=[], help="explicit interpretation to include in the report")
    parser.add_argument("--hypothesis", action="append", default=[], help="explicit unproven hypothesis to include in the report")
    parser.add_argument(
        "--view", choices=("changes", "snapshots", "both"), default="changes",
        help="table output: change log, post-edge snapshots, or both (snapshots require --clock)",
    )
    return parser


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Investigate FST/VCD waveforms with Verilog/SystemVerilog RTL.")
    parser.add_argument("--version", action="version", version=f"%(prog)s {TOOL_VERSION}")
    sub = parser.add_subparsers(dest="command", required=True)
    doctor = sub.add_parser("doctor", help="check waveform and RTL analysis capabilities")
    doctor.add_argument("--json", action="store_true")
    inspect = sub.add_parser("inspect", help="discover inputs and summarize waveform metadata")
    _add_wave_inputs(inspect)
    _add_source_inputs(inspect)
    inspect.add_argument("--json", action="store_true")
    inspect.add_argument("--verbose", action="store_true", help="include complete source and provenance records")
    scopes = sub.add_parser("scopes", help="list waveform scopes")
    _add_wave_inputs(scopes)
    scopes.add_argument("--scope")
    _add_match_inputs(scopes)
    scopes.add_argument("--json", action="store_true")
    signals = sub.add_parser("signals", help="discover waveform signals")
    _add_wave_inputs(signals)
    signals.add_argument("--scope")
    _add_match_inputs(signals)
    signals.add_argument("--limit", type=int, default=100)
    signals.add_argument("--json", action="store_true")
    authority = sub.add_parser("authority", help="build elaborated RTL ownership when available, otherwise static candidates")
    _add_wave_inputs(authority)
    _add_source_inputs(authority)
    authority.add_argument("--force", action="store_true")
    authority.add_argument("--authority-backend", choices=("auto", "verilator", "static"), default="auto")
    authority.add_argument("--summary-limit", type=int, default=8)
    authority.add_argument("--json", action="store_true")
    provenance = sub.add_parser("provenance", help="write a portable waveform and simulation-context manifest")
    _add_wave_inputs(provenance)
    _add_source_inputs(provenance)
    provenance.add_argument("--out", type=Path, help="manifest destination (default: build/wave-debug/provenance.json)")
    reproduce = sub.add_parser("reproduce", help="run an explicit simulation command and archive its evidence")
    _add_wave_inputs(reproduce)
    _add_source_inputs(reproduce)
    reproduce.add_argument("--run-command", required=True, help="explicit shell command to run in the workspace")
    reproduce.add_argument("--run-id", help="run output directory name")
    reproduce.add_argument("--testcase", help="testcase identifier recorded with the run")
    reproduce.add_argument("--results", type=Path, help="optional JUnit/XML results file produced by the command")
    reproduce.add_argument("--confirm-passing", action="store_true", help="explicitly confirm a successful run when no JUnit result is available")
    reproduce.add_argument("--out", type=Path, help="run manifest destination")
    case = sub.add_parser("case", help="create and validate immutable waveform-debug cases")
    case_sub = case.add_subparsers(dest="case_command", required=True)
    case_init = case_sub.add_parser("init", help="create an editable debug case from an explicit waveform")
    _add_wave_inputs(case_init)
    _add_source_inputs(case_init)
    case_init.add_argument("--case-id")
    case_init.add_argument("--out", type=Path, help="case destination (default: build/wave-debug/cases/<id>/case.json)")
    case_init.add_argument("--force", action="store_true", help="replace an existing output case")
    case_init.add_argument("--symptom", help="human-authored failure summary")
    case_init.add_argument("--symptom-start", help="optional symptom window start")
    case_init.add_argument("--symptom-end", help="optional symptom window end")
    case_init.add_argument("--affected-signal", action="append", default=[], help="exact waveform path affected by the symptom")
    case_init.add_argument("--first-divergence", help="optional reference to a compare result")
    case_validate = case_sub.add_parser("validate", help="validate case hypotheses and write a new revision")
    case_validate.add_argument("--case", type=Path, required=True)
    case_validate.add_argument("--workspace", type=Path, default=Path.cwd())
    case_validate.add_argument("--out-dir", type=Path)
    case_validate.add_argument("--out", type=Path, help="validation snapshot destination")
    case_validate.add_argument("--hypothesis", action="append", default=[], help="hypothesis id to validate (default: all)")
    case_validate.add_argument("--authority-db", type=Path)
    case_validate.add_argument("--max-changes", type=int, default=200)
    case_validate.add_argument("--report", type=Path, help="write a Markdown validation report")
    case_verify = case_sub.add_parser("verify-fix", help="verify a fixed waveform against a failing case")
    case_verify.add_argument("--case", type=Path, required=True)
    case_verify.add_argument("--waveform", type=Path, required=True, help="explicit waveform from the verification run")
    case_verify.add_argument("--verification-manifest", type=Path, required=True)
    case_verify.add_argument("--outcome", choices=("fixed",), required=True)
    case_verify.add_argument("--workspace", type=Path, default=Path.cwd())
    case_verify.add_argument("--out-dir", type=Path)
    case_verify.add_argument("--out", type=Path)
    case_verify.add_argument("--authority-db", type=Path)
    case_verify.add_argument("--max-changes", type=int, default=200)
    case_verify.add_argument("--report", type=Path)
    signal = sub.add_parser("signal", help="query one signal at a timestamp")
    _add_wave_inputs(signal)
    signal.add_argument("--signal", required=True, dest="signal_path")
    signal.add_argument("--time", required=True)
    signal.add_argument("--radix", choices=("auto", "hex", "bin", "dec", "signed"), default="auto")
    signal.add_argument("--window-len", help=argparse.SUPPRESS)
    signal.add_argument("--authority-db", type=Path)
    _probe_parser(sub, "probe", "query a compact bounded evidence window")
    packet = _probe_parser(sub, "packet", "write a compact evidence packet", window_required=False)
    packet.add_argument("--window", type=int)
    packet.add_argument("--window-len")
    compare = sub.add_parser("compare", help="find first divergence between two waveforms")
    compare.add_argument("good", type=Path)
    compare.add_argument("bad", type=Path)
    compare.add_argument("--workspace", type=Path, default=Path.cwd())
    compare.add_argument("--out-dir", type=Path)
    compare.add_argument("--scope")
    compare.add_argument("--match", action="append", default=[])
    compare.add_argument("--regex", action="append", default=[])
    compare.add_argument("--limit", type=int, default=64)
    compare.add_argument("--align", choices=("absolute", "reset-deassert", "clock-edge"), default="absolute")
    compare.add_argument("--align-signal", help="full waveform path used for non-absolute alignment")
    compare.add_argument("--align-occurrence", type=int, default=1, help="one-based matching reset release or clock edge")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "doctor":
            return _doctor(args.json)
        if args.command == "inspect":
            return _inspect(args)
        if args.command == "scopes":
            return _scopes(args)
        if args.command == "signals":
            return _signals(args)
        if args.command == "authority":
            return _authority(args)
        if args.command == "provenance":
            return _provenance(args)
        if args.command == "reproduce":
            return _reproduce(args)
        if args.command == "case":
            if args.case_command == "init":
                return _case_init(args)
            if args.case_command == "validate":
                return _case_validate(args)
            if args.case_command == "verify-fix":
                return _case_verify_fix(args)
        if args.command == "signal":
            return _signal(args)
        if args.command == "probe":
            if args.start is not None and args.end is None:
                raise ValueError("--start requires --end")
            return _probe(args)
        if args.command == "packet":
            if args.window is not None:
                if args.window_len is None:
                    raise ValueError("--window requires --window-len")
                _workspace, waveform, output, _ = _paths(args)
                wave = open_waveform(waveform, skill_root(), output)
                length = parse_time(args.window_len, wave.header.timescale)
                args.start, args.end, args.around = str(args.window * length), str((args.window + 1) * length - 1), None
            elif args.start is None and args.around is None and not args.around_failure:
                raise ValueError("packet requires --window/--window-len, --start/--end, or --around")
            elif args.start is not None and args.end is None:
                raise ValueError("--start requires --end")
            return _probe(args, True)
        if args.command == "compare":
            return _compare(args)
    except (ValueError, RuntimeError, OSError) as error:
        parser.exit(2, f"error: {error}\n")
    return 0
