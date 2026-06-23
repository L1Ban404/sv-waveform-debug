#!/usr/bin/env python3
"""Discover and query FST/VCD waveforms alongside Verilog/SystemVerilog RTL."""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import sys
from typing import Iterable


HDL_SUFFIXES = {".sv", ".v"}
WAVE_SUFFIXES = {".fst", ".vcd"}
SKIP_PARTS = {".git", ".codex", "node_modules", "wave-debug"}


def skill_root() -> Path:
    return Path(__file__).resolve().parents[1]


def engine_root() -> Path:
    path = skill_root() / "vendor/hardware-debug-skill"
    if not (path / "scripts/hw_debug_cli.py").is_file():
        raise SystemExit(
            "waveform engine is missing; run "
            "`git submodule update --init --recursive .codex/vendor/hardware-debug-skill`"
        )
    return path


def is_skipped(path: Path, root: Path) -> bool:
    try:
        parts = path.relative_to(root).parts
    except ValueError:
        return False
    return any(part in SKIP_PARTS for part in parts)


def discover_files(root: Path, suffixes: set[str]) -> list[Path]:
    return sorted(
        (
            path
            for path in root.rglob("*")
            if path.is_file() and path.suffix.lower() in suffixes and not is_skipped(path, root)
        ),
        key=lambda path: path.stat().st_mtime_ns,
        reverse=True,
    )


def resolve_waveform(workspace: Path, explicit: Path | None) -> tuple[Path, list[Path]]:
    if explicit is not None:
        path = explicit if explicit.is_absolute() else workspace / explicit
        if not path.is_file() or path.suffix.lower() not in WAVE_SUFFIXES:
            raise SystemExit(f"waveform must be an existing .fst or .vcd file: {path}")
        return path.resolve(), [path.resolve()]
    candidates = discover_files(workspace, WAVE_SUFFIXES)
    if not candidates:
        raise SystemExit(f"no .fst or .vcd waveform found under {workspace}")
    return candidates[0].resolve(), candidates


def source_files(source_root: Path) -> list[Path]:
    files = discover_files(source_root, HDL_SUFFIXES)
    if not files:
        raise SystemExit(f"no .sv or .v source found under {source_root}")
    return files


def module_candidates(files: Iterable[Path]) -> list[str]:
    declared: set[str] = set()
    instantiated: set[str] = set()
    module_re = re.compile(r"\bmodule\s+([A-Za-z_][A-Za-z0-9_$]*)")
    instance_re = re.compile(
        r"(?m)^\s*([A-Za-z_][A-Za-z0-9_$]*)\s+(?:#\s*\([^;]*?\)\s*)?"
        r"[A-Za-z_][A-Za-z0-9_$]*\s*\(",
        re.DOTALL,
    )
    for path in files:
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        text = re.sub(r"/\*.*?\*/", " ", text, flags=re.DOTALL)
        text = re.sub(r"//.*", " ", text)
        declared.update(module_re.findall(text))
        instantiated.update(match.group(1) for match in instance_re.finditer(text))
    roots = sorted(declared - instantiated)
    preferred = [name for name in roots if re.search(r"(?:^|_)(?:tb|test|top)$", name, re.I)]
    return preferred or roots


def vcd_root_scopes(waveform: Path) -> set[str]:
    if waveform.suffix.lower() != ".vcd":
        return set()
    scopes: set[str] = set()
    depth = 0
    try:
        with waveform.open("r", encoding="utf-8", errors="ignore") as handle:
            for line in handle:
                if "$enddefinitions" in line:
                    break
                if "$scope" in line:
                    match = re.search(r"\$scope\s+\S+\s+(\S+)\s+\$end", line)
                    if match and depth == 0:
                        scopes.add(match.group(1))
                    depth += 1
                elif "$upscope" in line:
                    depth = max(0, depth - 1)
    except OSError:
        return set()
    return scopes


def inferred_top(candidates: list[str], waveform: Path) -> str | None:
    scope_matches = sorted(set(candidates) & vcd_root_scopes(waveform))
    if len(scope_matches) == 1:
        return scope_matches[0]
    path_text = waveform.as_posix().lower()
    scored: list[tuple[int, str]] = []
    for candidate in candidates:
        lowered = candidate.lower()
        stems = {lowered, re.sub(r"(?:_?(?:tb|test|top))$", "", lowered)}
        score = max((len(stem) for stem in stems if stem and stem in path_text), default=0)
        if score:
            scored.append((score, candidate))
    if scored:
        best = max(score for score, _name in scored)
        winners = sorted(name for score, name in scored if score == best)
        if len(winners) == 1:
            return winners[0]
    return candidates[0] if len(candidates) == 1 else None


def resolve_top(explicit: str | None, candidates: list[str], waveform: Path) -> str:
    if explicit:
        return explicit
    inferred = inferred_top(candidates, waveform)
    if inferred:
        return inferred
    if not candidates:
        raise SystemExit("could not infer a top module; pass --top")
    rendered = "\n  - ".join(candidates[:20])
    raise SystemExit(f"multiple top-module candidates; pass --top:\n  - {rendered}")


def cache_root(workspace: Path, explicit: Path | None) -> Path:
    if explicit is None:
        return workspace / "build/wave-debug"
    return explicit if explicit.is_absolute() else workspace / explicit


def analysis_waveform(source: Path, output_root: Path) -> Path:
    if source.suffix.lower() != ".fst":
        return source
    converter = shutil.which("fst2vcd")
    if converter is None:
        return source
    converted = output_root / "converted" / f"{source.stem}.vcd"
    if converted.is_file() and converted.stat().st_mtime_ns >= source.stat().st_mtime_ns:
        return converted
    converted.parent.mkdir(parents=True, exist_ok=True)
    temporary = converted.with_suffix(".vcd.tmp")
    with temporary.open("wb") as output:
        subprocess.run([converter, str(source)], stdout=output, check=True)
    temporary.replace(converted)
    return converted


def configure_engine(engine: Path) -> None:
    scripts = engine / "scripts"
    if str(scripts) not in sys.path:
        sys.path.insert(0, str(scripts))
    if importlib.util.find_spec("pywellen") is None:
        vendored = engine / "wellen/pywellen"
        if str(vendored) not in sys.path:
            sys.path.insert(0, str(vendored))


def waveform_api(engine: Path):
    configure_engine(engine)
    from lib.query_waveform_wellen import (  # pylint: disable=import-outside-toplevel
        build_debug_packet_from_waveform,
        query_signal_value_from_waveform,
    )

    return build_debug_packet_from_waveform, query_signal_value_from_waveform


def normalized_sources(sources: Path, files: list[Path], output: Path) -> Path:
    normalized = output / "normalized-rtl"
    for source in files:
        relative = source.relative_to(sources)
        target = normalized / relative
        text = source.read_text(encoding="utf-8", errors="ignore")
        text = re.sub(
            r"(\bmodule\s+[A-Za-z_][A-Za-z0-9_$]*)\s*;",
            r"\1();",
            text,
        )
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.is_file() or target.read_text(encoding="utf-8") != text:
            target.write_text(text, encoding="utf-8")
    return normalized


def restore_source_paths(authority_dir: Path, normalized: Path, sources: Path) -> None:
    old = str(normalized.resolve())
    new = str(sources.resolve())
    for name in ("rtl_authority_table.json", "rtl_authority_index.json"):
        path = authority_dir / name
        if path.is_file():
            path.write_text(path.read_text(encoding="utf-8").replace(old, new), encoding="utf-8")
    database = authority_dir / "rtl_authority.sqlite3"
    if database.is_file():
        with sqlite3.connect(database) as connection:
            connection.execute(
                "update authority_lookup set source_file = replace(source_file, ?, ?)",
                (old, new),
            )


def run_authority(
    engine: Path,
    sources: Path,
    files: list[Path],
    top: str,
    output: Path,
    force: bool,
) -> None:
    normalized = normalized_sources(sources, files, output.parent)
    command = [
        sys.executable,
        str(engine / "scripts/hw_debug_cli.py"),
        "build-authority",
        "--rtl-root",
        str(normalized),
        "--top",
        top,
        "--out-dir",
        str(output),
    ]
    if force:
        command.append("--force")
    env = os.environ.copy()
    vendored = engine / "wellen/pywellen"
    env["PYTHONPATH"] = os.pathsep.join(filter(None, (str(vendored), env.get("PYTHONPATH"))))
    subprocess.run(command, check=True, env=env)
    restore_source_paths(output, normalized, sources)


def add_inputs(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    parser.add_argument("--waveform", type=Path)
    parser.add_argument("--source-root", type=Path)
    parser.add_argument("--top")
    parser.add_argument("--out-dir", type=Path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    inspect_parser = sub.add_parser("inspect", help="discover waveform, HDL, and top candidates")
    add_inputs(inspect_parser)
    authority_parser = sub.add_parser("authority", help="build exact RTL hierarchy ownership")
    add_inputs(authority_parser)
    authority_parser.add_argument("--force", action="store_true")
    packet_parser = sub.add_parser("packet", help="query a bounded waveform window")
    add_inputs(packet_parser)
    packet_parser.add_argument("--window", required=True, type=int)
    packet_parser.add_argument("--window-len", type=int, default=1000)
    packet_parser.add_argument("--focus-scope")
    signal_parser = sub.add_parser("signal", help="query one signal at a timestamp")
    add_inputs(signal_parser)
    signal_parser.add_argument("--signal", required=True, dest="signal_path")
    signal_parser.add_argument("--time", required=True, type=int)
    signal_parser.add_argument("--window-len", type=int, default=1000)

    args = parser.parse_args(argv)
    workspace = args.workspace.resolve()
    sources = (args.source_root or workspace)
    sources = sources if sources.is_absolute() else workspace / sources
    files = source_files(sources)
    tops = module_candidates(files)
    waveform, waveforms = resolve_waveform(workspace, args.waveform)
    output = cache_root(workspace, args.out_dir)

    if args.command == "inspect":
        print(f"workspace: {workspace}")
        print(f"waveform: {waveform}")
        print(f"waveform-format: {waveform.suffix.lower()[1:]}")
        print(f"waveform-candidates: {len(waveforms)}")
        for candidate in waveforms[:10]:
            print(f"  - {candidate}")
        print(f"source-root: {sources.resolve()}")
        print(f"hdl-files: {len(files)}")
        print(f"top-candidates: {', '.join(tops) if tops else '<none>'}")
        print(f"selected-top: {args.top or inferred_top(tops, waveform) or '<ambiguous>'}")
        print(f"output-dir: {output}")
        return 0

    top = resolve_top(args.top, tops, waveform)
    authority_dir = output / "authority" / top
    authority_db = authority_dir / "rtl_authority.sqlite3"
    engine = engine_root()
    if args.command == "authority":
        run_authority(engine, sources, files, top, authority_dir, args.force)
        return 0
    if not authority_db.is_file() and args.command == "packet":
        raise SystemExit(f"RTL authority missing; run `authority --top {top}` first")

    readable = analysis_waveform(waveform, output)
    build_packet, query_signal = waveform_api(engine)
    metadata_cache = output / "cache/waveform_meta"
    query_cache = output / "cache/waveform_query"
    if args.command == "packet":
        result = build_packet(
            wave_path=readable,
            authority_db=authority_db,
            window_id=f"w{args.window}",
            window_len=args.window_len,
            focus_scope=args.focus_scope or f"TOP.{top}",
            metadata_cache_root=metadata_cache,
            query_cache_root=query_cache,
        )
        packet_path = output / "packets" / f"packet_w{args.window}.json"
        packet_path.parent.mkdir(parents=True, exist_ok=True)
        packet_path.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        print(packet_path)
        return 0

    result = query_signal(
        wave_path=readable,
        full_wave_path=args.signal_path,
        t=args.time,
        window_len=args.window_len,
        metadata_cache_root=metadata_cache,
        query_cache_root=query_cache,
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
