#!/usr/bin/env python3
from __future__ import annotations

from pathlib import Path
import re

import yaml


ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    text = (ROOT / "SKILL.md").read_text(encoding="utf-8")
    match = re.match(r"^---\n(.*?)\n---\n", text, re.DOTALL)
    if not match:
        raise SystemExit("SKILL.md frontmatter is missing")
    frontmatter = yaml.safe_load(match.group(1))
    if set(frontmatter) != {"name", "description"}:
        raise SystemExit("SKILL.md frontmatter must contain only name and description")
    if frontmatter["name"] != ROOT.name:
        raise SystemExit("skill name must match directory name")
    if not re.fullmatch(r"[a-z0-9-]{1,63}", frontmatter["name"]):
        raise SystemExit("skill name is invalid")
    metadata = yaml.safe_load((ROOT / "agents/openai.yaml").read_text(encoding="utf-8"))
    interface = metadata["interface"]
    if not 25 <= len(interface["short_description"]) <= 64:
        raise SystemExit("short_description must contain 25-64 characters")
    if f"${frontmatter['name']}" not in interface["default_prompt"]:
        raise SystemExit("default_prompt must name the skill")
    retired_name = "hardware" + "-debug-skill"
    forbidden = (f"vendor/{retired_name}", f"trace1729/{retired_name}")
    paths = [ROOT / "README.md", ROOT / "SKILL.md"]
    paths.extend((ROOT / "scripts").glob("**/*"))
    for path in paths:
        if path.is_file() and path.suffix in {".md", ".py"}:
            contents = path.read_text(encoding="utf-8", errors="ignore")
            if any(reference in contents for reference in forbidden):
                raise SystemExit(f"retired submodule reference remains in {path.relative_to(ROOT)}")
    print("skill metadata valid")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
