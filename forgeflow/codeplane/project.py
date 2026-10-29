"""Read project check declarations without running repository code."""

from __future__ import annotations

import hashlib
import json
import re
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
    # Audit #7 — a package.json script body is repository-controlled
    # content, not a platform command. Selecting "npm run <name>" by
    # name alone would execute whatever body the repo wrote, in a
    # shell, with no allow-list. A body is only selected when it is a
    # plain single command with no shell chaining, redirection,
    # substitution, or network/file-deletion tooling. Rejected scripts
    # are reported in "skipped", never silently run and never silently
    # replaced by something else.
    _dangerous = re.compile(r"[;|&><`$(){}\\\n\r]")
    _dangerous_words = {
        "curl",
        "wget",
        "nc",
        "ssh",
        "scp",
        "ftp",
        "rm",
        "chmod",
        "chown",
        "sudo",
        "su",
    }
    selected = []
    skipped = []
    for alternatives in (("typecheck", "type-check"), ("test",), ("build",)):
        name = next(
            (
                key
                for key in alternatives
                if isinstance(scripts.get(key), str) and scripts[key].strip()
            ),
            None,
        )
        if not name:
            continue
        body = str(scripts[name]).strip()
        words = set(re.findall(r"[A-Za-z0-9_-]+", body))
        if _dangerous.search(body) or words & _dangerous_words:
            skipped.append(
                {
                    "name": name,
                    "reason": "script body contains shell chaining or dangerous tooling; not selected",
                }
            )
            continue
        suffix = " -- --run" if name == "test" and "vitest" in body and "--run" not in body else ""
        selected.append(
            {"kind": "test" if name == "test" else name, "command": f"npm run {name}{suffix}"}
        )
    return {
        "source": "package.json",
        "content_hash": hashlib.sha256(content).hexdigest(),
        "checks": selected,
        "skipped": skipped,
        "test_declared": any(row["kind"] == "test" for row in selected),
        "command": " && ".join(row["command"] for row in selected),
        "navigation": "text_search",
        "language_service": "not_detected",
    }
