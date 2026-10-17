"""Pinned-source, real-model benchmark in its own disposable PostgreSQL database."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import secrets
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path
from urllib.parse import quote, urlsplit

import httpx
import psycopg
from dotenv import dotenv_values
from langgraph.checkpoint.postgres import PostgresSaver
from psycopg import sql

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from professional_benchmark_cases import cases
from run_tests import database_url

from forgeflow.evaluation.artifact_checks import check_artifacts, run_verdict


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--variant", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--port", type=int, default=8011)
    parser.add_argument("--repetitions", type=int, default=3)
    parser.add_argument("--domain", choices=["code", "document", "research"])
    parser.add_argument("--case", help="One manifest case for a targeted real-model diagnostic")
    options = parser.parse_args()
    if not 1 <= options.repetitions <= 3:
        raise ValueError("Benchmark repetitions must be between one and three")
    root = Path(__file__).resolve().parents[1]
    options.root = options.root.resolve()
    assert (options.root / "forgeflow/api/main.py").is_file()
    options.output.parent.mkdir(parents=True, exist_ok=True)
    manifest = options.output.parent / "cases.json"
    if not manifest.exists():
        dataset = [
            {
                **case,
                "files": [[name, base64.b64encode(data).decode()] for name, data in case["files"]],
            }
            for case in cases()
        ]
        manifest.write_text(json.dumps(dataset, ensure_ascii=False, indent=2), encoding="utf-8")
    dataset = json.loads(manifest.read_text(encoding="utf-8"))
    if options.domain:
        dataset = [case for case in dataset if case["domain"] == options.domain]
    if options.case:
        dataset = [case for case in dataset if case["id"] == options.case]
        if not dataset:
            raise ValueError("Unknown benchmark case")
    values = dotenv_values(root / ".env")
    env = {**os.environ, **{k: v for k, v in values.items() if v is not None}}
    admin = env["POSTGRES_MIGRATION_URL"]
    runtime = env["POSTGRES_URL"]
    name = "forgeflow_benchmark_" + uuid.uuid4().hex
    password = secrets.token_urlsafe(32)
    tenant = "bench-" + uuid.uuid4().hex
    runtime_url = database_url(runtime, name)
    migration_url = database_url(admin, name)
    resources = tempfile.mkdtemp(prefix="forgeflow-benchmark-resources-")
    env.update(
        POSTGRES_URL=runtime_url,
        POSTGRES_SYNC_URL=runtime_url,
        POSTGRES_MIGRATION_URL=migration_url,
        RESOURCE_STORE_ROOT=resources,
        STORAGE_BACKEND="postgres",
        DEFAULT_TENANT_ID=tenant,
        DEV_LOGIN_ENABLED="true",
        DEV_LOGIN_PASSWORD=password,
        AGENT_RUNTIME_MODE="react",
        LLM_PROVIDER="ollama",
        OLLAMA_MODEL="qwen3:8b",
        OLLAMA_MODEL_STRONG="qwen3:8b",
        OLLAMA_NUM_CTX="8192",
        OLLAMA_THINK="false",
        PYTHONUTF8="1",
        NO_PROXY="127.0.0.1,localhost",
    )
    for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        env.pop(key, None)
    digest = hashlib.sha256()
    for path in sorted((options.root / "forgeflow").rglob("*.py")):
        digest.update(str(path.relative_to(options.root)).encode())
        digest.update(path.read_bytes())
    report = {
        "variant": options.variant,
        "source_hash": digest.hexdigest(),
        "dataset_hash": hashlib.sha256(manifest.read_bytes()).hexdigest(),
        "provenance": "independent_synthetic_regression",
        "model": "qwen3:8b",
        "temperature": 0,
        "num_ctx": 8192,
        "think": False,
        "repetitions": options.repetitions,
        "cases": [],
    }
    with httpx.Client(trust_env=False, timeout=20) as model_client:
        tags = model_client.get("http://127.0.0.1:11434/api/tags")
        tags.raise_for_status()
        report["model_digest"] = next(
            m["digest"] for m in tags.json()["models"] if m["name"] == "qwen3:8b"
        )

    def save():
        options.output.write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    save()
    with psycopg.connect(admin, autocommit=True) as control:
        control.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
        server = None
        logfile = Path(tempfile.gettempdir()) / (name + ".log")
        try:
            subprocess.run(
                [sys.executable, "-m", "alembic", "upgrade", "head"],
                cwd=root,
                env=env,
                check=True,
                stdout=subprocess.DEVNULL,
            )
            with PostgresSaver.from_conn_string(migration_url) as saver:
                saver.setup()
            role = urlsplit(runtime_url).username
            with psycopg.connect(migration_url) as conn:
                permissions = conn.execute(
                    "SELECT rolsuper,rolbypassrls FROM pg_roles WHERE rolname=%s", (role,)
                ).fetchone()
                assert permissions and not any(permissions)
                for grant in (
                    "GRANT USAGE ON SCHEMA public TO {}",
                    "GRANT SELECT,INSERT,UPDATE,DELETE ON ALL TABLES IN SCHEMA public TO {}",
                    "GRANT USAGE,SELECT ON ALL SEQUENCES IN SCHEMA public TO {}",
                ):
                    conn.execute(sql.SQL(grant).format(sql.Identifier(role)))
            with logfile.open("w", encoding="utf-8") as log:
                launch = f"from forgeflow.bootstrap import run_api_server; run_api_server('forgeflow.api.main:app',host='127.0.0.1',port={options.port},reload=False,log_level='warning')"
                server = subprocess.Popen(
                    [sys.executable, "-c", launch],
                    cwd=options.root,
                    env=env,
                    stdout=log,
                    stderr=log,
                )
                with httpx.Client(
                    base_url=f"http://127.0.0.1:{options.port}", trust_env=False, timeout=30
                ) as client:
                    deadline = time.monotonic() + 100
                    while True:
                        if server.poll() is not None:
                            raise RuntimeError(
                                "Benchmark API startup failed; inspect the local isolated-server log"
                            )
                        try:
                            if client.get("/health").status_code == 200:
                                break
                        except httpx.HTTPError:
                            pass
                        if time.monotonic() > deadline:
                            raise TimeoutError("Benchmark API not ready")
                        time.sleep(0.5)
                    login = client.post(
                        "/auth/login",
                        json={"user_id": "manager-1", "password": password, "workspace_id": tenant},
                    )
                    login.raise_for_status()
                    client.headers["Authorization"] = "Bearer " + login.json()["access_token"]
                    for repetition in range(options.repetitions):
                        for case in dataset:
                            started = time.monotonic()
                            row = {
                                "case_id": case["id"],
                                "domain": case["domain"],
                                "repetition": repetition + 1,
                            }
                            try:
                                ids = []
                                for filename, encoded in case["files"]:
                                    upload = client.post(
                                        "/resources/files",
                                        files={"file": (filename, base64.b64decode(encoded))},
                                    )
                                    upload.raise_for_status()
                                    ids.append(upload.json()["id"])
                                admission = client.post(
                                    "/workspace/tasks",
                                    json={"intent": case["intent"], "context": {"resources": ids}},
                                )
                                admission.raise_for_status()
                                run_id = admission.json()["run_id"]
                                row["run_id"] = run_id
                                deadline = time.monotonic() + 180
                                while True:
                                    response = client.get("/runs/" + run_id)
                                    response.raise_for_status()
                                    record = response.json()
                                    if record["status"] not in {"running", "queued", "pending"}:
                                        break
                                    if time.monotonic() > deadline:
                                        client.post("/runs/" + run_id + "/abort")
                                        raise TimeoutError(
                                            "Benchmark task exceeded 180 seconds; abort requested"
                                        )
                                    time.sleep(0.5)
                                artifacts = []
                                for artifact in record.get("artifacts", []):
                                    if artifact.get("content_ref"):
                                        data = client.get(
                                            "/runs/"
                                            + run_id
                                            + "/artifacts/"
                                            + quote(artifact["id"], safe="")
                                        )
                                        data.raise_for_status()
                                        artifacts.append(
                                            (artifact.get("format", "text"), data.content)
                                        )
                                    elif artifact.get("kind") != "report_markdown" and artifact.get(
                                        "content"
                                    ):
                                        artifacts.append(
                                            (
                                                artifact.get("format", "text"),
                                                artifact["content"].encode(),
                                            )
                                        )
                                artifact_folder = (
                                    options.output.parent
                                    / (options.variant + "-artifacts")
                                    / (str(repetition + 1) + "-" + case["id"])
                                )
                                artifact_folder.mkdir(parents=True, exist_ok=True)
                                row["artifact_hashes"] = []
                                for artifact_index, (artifact_format, artifact_data) in enumerate(
                                    artifacts
                                ):
                                    suffix = (
                                        artifact_format
                                        if artifact_format
                                        in {"text", "markdown", "txt", "md", "docx", "xlsx"}
                                        else "bin"
                                    )
                                    artifact_path = artifact_folder / f"{artifact_index}.{suffix}"
                                    artifact_path.write_bytes(artifact_data)
                                    row["artifact_hashes"].append(
                                        {
                                            "path": str(
                                                artifact_path.relative_to(options.output.parent)
                                            ),
                                            "sha256": hashlib.sha256(artifact_data).hexdigest(),
                                        }
                                    )
                                checks = check_artifacts(
                                    artifacts,
                                    case["assertions"],
                                    record.get("tool_invocations", []),
                                )
                                row.update(
                                    status=record["status"],
                                    outcome=record.get("outcome"),
                                    checks=checks,
                                    passed=run_verdict(record, checks),
                                    false_completion=record.get("outcome") == "success"
                                    and not all(checks.values()),
                                    total_tokens=record.get("total_tokens"),
                                    budget=record.get("llm", {}).get("execution_budget"),
                                    errors=record.get("errors", []),
                                )
                            except Exception as exc:
                                row.update(
                                    passed=False, false_completion=False, error=str(exc)[:250]
                                )
                            row["seconds"] = round(time.monotonic() - started, 3)
                            report["cases"].append(row)
                            save()
                            print(
                                json.dumps(
                                    {
                                        "variant": options.variant,
                                        "done": len(report["cases"]),
                                        "case": case["id"],
                                        "passed": row["passed"],
                                    }
                                ),
                                flush=True,
                            )
        finally:
            if server is not None and server.poll() is None:
                server.terminate()
                try:
                    server.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    server.kill()
                    server.wait(timeout=10)
            assert (
                name.startswith("forgeflow_benchmark_")
                and len(name) == len("forgeflow_benchmark_") + 32
            )
            control.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))
    report["complete"] = len(report["cases"]) == len(dataset) * options.repetitions
    report["summary"] = {
        domain: {
            "tasks": len(rows := [r for r in report["cases"] if r["domain"] == domain]),
            "passed": sum(r["passed"] for r in rows),
            "false_completions": sum(r["false_completion"] for r in rows),
        }
        for domain in {c["domain"] for c in dataset}
    }
    save()


if __name__ == "__main__":
    main()
