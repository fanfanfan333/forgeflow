"""Generate the empty-response container contract from actual FastAPI OpenAPI."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from forgeflow.api.main import app


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    paths = app.openapi()["paths"]
    arrays = []
    for path, operations in paths.items():
        response = operations.get("get", {}).get("responses", {}).get("200", {})
        schema = response.get("content", {}).get("application/json", {}).get("schema", {})
        if schema.get("type") == "array":
            arrays.append(path)
    content = (
        "// Generated from FastAPI OpenAPI.\nconst arrayPaths = "
        + json.dumps(sorted(arrays), ensure_ascii=False, indent=2)
        + " as const\nexport default arrayPaths\n"
    )
    target = Path(__file__).resolve().parents[1] / "frontend/e2e/api-array-paths.generated.ts"
    if args.check:
        if target.read_text(encoding="utf-8") != content:
            raise SystemExit(
                "API fixture contract drifted; run scripts/export_api_fixture_contract.py"
            )
    else:
        target.write_text(content, encoding="utf-8")


if __name__ == "__main__":
    main()
