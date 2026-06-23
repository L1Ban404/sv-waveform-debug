from __future__ import annotations

import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys


def engine_root(skill_root: Path) -> Path:
    path = skill_root / "vendor/hardware-debug-skill"
    if not (path / "scripts/hw_debug_cli.py").is_file():
        raise RuntimeError(
            "waveform engine is missing; run `git submodule update --init --recursive "
            ".codex/skills/systemverilog-waveform-debug-skill`"
        )
    return path


def normalized_sources(source_root: Path, files: list[Path], output: Path) -> Path:
    normalized = output / "normalized-rtl"
    for source in files:
        try:
            relative = source.relative_to(source_root)
        except ValueError:
            relative = Path("external") / source.name
        target = normalized / relative
        text = source.read_text(encoding="utf-8", errors="ignore")
        text = re.sub(r"(\bmodule\s+[A-Za-z_][A-Za-z0-9_$]*)\s*;", r"\1();", text)
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.is_file() or target.read_text(encoding="utf-8") != text:
            target.write_text(text, encoding="utf-8")
    return normalized


def _restore_paths(authority_dir: Path, normalized: Path, sources: Path) -> None:
    old, new = str(normalized.resolve()), str(sources.resolve())
    for name in ("rtl_authority_table.json", "rtl_authority_index.json"):
        path = authority_dir / name
        if path.is_file():
            path.write_text(path.read_text(encoding="utf-8").replace(old, new), encoding="utf-8")
    database = authority_dir / "rtl_authority.sqlite3"
    if database.is_file():
        with sqlite3.connect(database) as connection:
            connection.execute("update authority_lookup set source_file = replace(source_file, ?, ?)", (old, new))


def build_authority(skill_root: Path, source_root: Path, files: list[Path], top: str, output: Path, force: bool) -> None:
    engine = engine_root(skill_root)
    normalized = normalized_sources(source_root, files, output.parent)
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
    _restore_paths(output, normalized, source_root)


def _source_context(source_file: str | None, local_name: str | None, limit: int = 6) -> list[dict[str, object]]:
    if not source_file or not local_name:
        return []
    path = Path(source_file)
    if not path.is_file():
        return []
    pattern = re.compile(rf"\b{re.escape(local_name)}\b")
    rows: list[dict[str, object]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8", errors="ignore").splitlines(), 1):
        if pattern.search(line):
            rows.append({"file": str(path), "line": line_number, "text": line.strip(), "provenance": "heuristic-text-match"})
            if len(rows) >= limit:
                break
    return rows


def lookup_authority(database: Path | None, paths: list[str]) -> dict[str, dict[str, object]]:
    if database is None or not database.is_file() or not paths:
        return {}
    candidates = list(dict.fromkeys(paths + [path[4:] for path in paths if path.startswith("TOP.")]))
    placeholders = ",".join("?" for _ in candidates)
    with sqlite3.connect(database) as connection:
        connection.row_factory = sqlite3.Row
        columns = {row[1] for row in connection.execute("pragma table_info(authority_lookup)")}
        selected = [
            name for name in (
                "full_signal_name", "module_type", "instance_path", "local_signal_name",
                "signal_kind", "direction", "decl_width_bits", "source_file", "provenance",
            ) if name in columns
        ]
        rows = connection.execute(
            f"select {', '.join(selected)} from authority_lookup where full_signal_name in ({placeholders})",
            candidates,
        ).fetchall()
    result: dict[str, dict[str, object]] = {}
    for row in rows:
        item = dict(row)
        item["match_status"] = "exact"
        item["source_context"] = _source_context(item.get("source_file"), item.get("local_signal_name"))
        result[str(item["full_signal_name"])] = item
        result[f"TOP.{item['full_signal_name']}"] = item
    return result


def authority_fingerprint(database: Path | None) -> dict[str, object] | None:
    if database is None or not database.is_file():
        return None
    stat = database.stat()
    return {"path": str(database.resolve()), "size": stat.st_size, "mtime_ns": stat.st_mtime_ns}
