"""Provision least-privilege local OV identities; never print keys."""

import hashlib
import json
import time
from pathlib import Path

import httpx


def main():
    for _ in range(30):
        try:
            health = httpx.get("http://127.0.0.1:1933/health", trust_env=False, timeout=2)
            if health.is_success and health.json().get("healthy"):
                break
        except httpx.HTTPError:
            pass
        time.sleep(1)
    else:
        raise RuntimeError("OpenViking is not ready for user provisioning")
    root = Path(".runtime/openviking")
    config = json.loads((root / "ov.conf").read_text(encoding="utf-8"))
    mapping_path = root / "integration.json"
    mapping = (
        json.loads(mapping_path.read_text(encoding="utf-8"))
        if mapping_path.exists()
        else {
            "OPENVIKING_URL": "http://127.0.0.1:1933",
            "OPENVIKING_SYNC_ENABLED": "true",
            "OPENVIKING_TIMEOUT_SECONDS": "15",
            "OPENVIKING_USER_KEYS": {},
        }
    )
    tenant = "default"
    account = "ff-" + hashlib.sha256(tenant.encode()).hexdigest()[:24]
    with httpx.Client(
        base_url="http://127.0.0.1:1933",
        headers={"X-API-Key": config["server"]["root_api_key"]},
        timeout=180,
        trust_env=False,
    ) as client:
        result = client.post(
            "/api/v1/admin/accounts", json={"account_id": account, "admin_user_id": "provisioner"}
        )
        if result.status_code not in (200, 201, 409):
            result.raise_for_status()
        for user in ("admin", "manager-1", "rep-1", "viewer-1"):
            if f"{tenant}:{user}" in mapping["OPENVIKING_USER_KEYS"]:
                continue
            response = client.post(
                f"/api/v1/admin/accounts/{account}/users", json={"user_id": user, "role": "user"}
            )
            response.raise_for_status()
            mapping["OPENVIKING_USER_KEYS"][f"{tenant}:{user}"] = response.json()["result"][
                "user_key"
            ]
            mapping_path.write_text(json.dumps(mapping, indent=2), encoding="utf-8")
    print("Provisioned 4 isolated user keys; root credential is not given to ForgeFlow.")


if __name__ == "__main__":
    main()
