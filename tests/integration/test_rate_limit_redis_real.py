"""Opt-in real Redis check; only a random test bucket is touched."""

import os
import subprocess
import sys
import uuid

import pytest


@pytest.mark.skipif(not os.getenv("RATE_LIMIT_REDIS_REAL"), reason="explicit real Redis acceptance")
def test_rate_limit_is_shared_across_processes():
    env = dict(os.environ, RATE_LIMIT_STORAGE_URI=os.environ["RATE_LIMIT_REDIS_REAL"])
    key = "forgeflow-acceptance-" + uuid.uuid4().hex
    script = (
        "import asyncio; from forgeflow.security.rate_limiter import consume; "
        f"print(asyncio.run(consume({key!r}, 2)))"
    )
    results = [
        subprocess.check_output([sys.executable, "-c", script], env=env, text=True).strip()
        for _ in range(3)
    ]
    assert results == ["True", "True", "False"]
