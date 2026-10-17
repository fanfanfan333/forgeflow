"""Read project check declarations without running repository code."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any


def project_checks(workspace: str) -> dict[str, Any]:
    root = Path(workspace)
    package = root / "package.json"
    if not package.is_file():
        return {}
    if package.is_symlink() or package.stat().st_size > 262144:
        raise ValueError("Project package.json must be a bounded regular file")
    content = package.read_bytes()
    try:
        metadata = json.loads(content)
    except (ValueError, UnicodeError) as exc:
        raise ValueError("Project package.json is invalid; cannot select checks") from exc
    if not isinstance(metadata, dict) or not isinstance(metadata.get("scripts", {}), dict):
        raise ValueError("Project scripts must be an object")
    scripts = metadata.get("scripts", {})
    selected = []
    for alternatives in (("typecheck", "type-check"), ("test",), ("build",)):
        name = next((key for key in alternatives if isinstance(scripts.get(key), str) and scripts[key].strip()), None)
        if name:
            suffix = " -- --run" if name == "test" and "vitest" in scripts[name] and "--run" not in scripts[name] else ""
            selected.append({"kind": "test" if name == "test" else name, "command": f"npm run {name}{suffix}"})
    return {"source": "package.json", "content_hash": hashlib.sha256(content).hexdigest(),
            "checks": selected, "test_declared": any(row["kind"] == "test" for row in selected),
            "command": " && ".join(row["command"] for row in selected),
            "navigation": "text_search", "language_service": "not_detected"}
