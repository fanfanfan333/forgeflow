"""Versioned bundled skills, imported through the same untrusted-package path."""

from pathlib import Path

from forgeflow.skills.package_import import package_spec

_ROOT = Path(__file__).with_name("builtins")


def builtin_packages() -> list[dict]:
    result = []
    for folder in sorted(_ROOT.iterdir()):
        if folder.is_dir() and (folder / "SKILL.md").is_file():
            files = {
                str(path.relative_to(folder)).replace("\\", "/"): path.read_text(encoding="utf-8")
                for path in sorted(folder.rglob("*"))
                if path.is_file()
            }
            spec = package_spec(files)
            result.append(
                {
                    "slug": folder.name,
                    "name": next(
                        (
                            line[2:].strip()
                            for line in files["SKILL.md"].splitlines()
                            if line.startswith("# ")
                        ),
                        folder.name,
                    ),
                    "version": "1.0.0",
                    "hash": spec["package_hash"],
                    "description": spec["manifest"]["description"],
                    "files": files,
                    "verification": "synthetic_cases_only",
                }
            )
    return result
