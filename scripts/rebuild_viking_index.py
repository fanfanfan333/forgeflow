"""Rebuild a derived OV index in a fresh volume, preserving the old container/data."""

import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path


def main():
    root = Path(".runtime/openviking").resolve()
    generation = datetime.now(UTC).strftime("%Y%m%d%H%M%S")
    volume = "forgeflow-openviking-data-" + generation
    backup = "forgeflow-openviking-backup-" + generation
    mapping_path = root / "integration.json"
    mapping = json.loads(mapping_path.read_text(encoding="utf-8"))
    (root / ("integration-backup-" + generation + ".json")).write_text(
        json.dumps(mapping), encoding="utf-8"
    )
    subprocess.run(["docker", "stop", "forgeflow-openviking"], check=True)
    subprocess.run(["docker", "rename", "forgeflow-openviking", backup], check=True)
    subprocess.run(["docker", "volume", "create", volume], check=True)
    digest = (root / "image-digest.txt").read_text(encoding="utf-8").strip()
    subprocess.run(
        [
            "docker",
            "run",
            "-d",
            "--name",
            "forgeflow-openviking",
            "--restart",
            "unless-stopped",
            "-p",
            "127.0.0.1:1933:1933",
            "-v",
            volume + ":/data",
            "-v",
            str(root / "ov.conf") + ":/app/.openviking/ov.conf:ro",
            "-e",
            "OPENVIKING_CONFIG_FILE=/app/.openviking/ov.conf",
            digest,
        ],
        check=True,
    )
    (root / "volume-name.txt").write_text(volume, encoding="utf-8")
    source = Path(
        mapping.get("OPENVIKING_OUTBOX_ROOT", str(Path.home() / ".forgeflow/openviking-outbox"))
    )
    destination = root / "outbox"
    destination.mkdir(exist_ok=True)
    for path in source.glob("*.json"):
        job = json.loads(path.read_text(encoding="utf-8"))
        if (
            f"{job['tenant']}:{job['user']}" not in mapping["OPENVIKING_USER_KEYS"]
            or job["status"] == "cancelled"
        ):
            continue
        job["history"] = (
            job.get("history", [])
            + [
                {
                    "status": job["status"],
                    "result": job.get("result"),
                    "generation": "before-" + generation,
                }
            ]
        )[-10:]
        job["status"], job["attempts"], job["generation"] = "pending", 0, generation
        for key in ("result", "failed_tasks", "error", "next_attempt_at"):
            job.pop(key, None)
        (destination / path.name).write_text(json.dumps(job, ensure_ascii=False), encoding="utf-8")
    mapping["OPENVIKING_OUTBOX_ROOT"] = str(destination)
    mapping["OPENVIKING_USER_KEYS"] = {}
    mapping_path.write_text(json.dumps(mapping, indent=2), encoding="utf-8")
    print(json.dumps({"previous_container_preserved": backup, "new_index_volume": volume}))


if __name__ == "__main__":
    main()
