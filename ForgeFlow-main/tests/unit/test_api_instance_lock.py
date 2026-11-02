import os
import subprocess
import sys

import pytest

from forgeflow.runtime.instance_lock import api_instance_lock


def test_shared_store_rejects_another_process_and_releases_on_exit(tmp_path):
    script = (
        "from pathlib import Path; from forgeflow.runtime.instance_lock import api_instance_lock; "
        "import sys; "
        "lock=api_instance_lock(Path(sys.argv[1])); lock.__enter__(); "
        "print('locked',flush=True); sys.stdin.readline(); lock.__exit__(None,None,None)"
    )
    child = subprocess.Popen(
        [sys.executable, "-c", script, str(tmp_path)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env={**os.environ, "PYTHONUNBUFFERED": "1"},
    )
    try:
        assert child.stdout.readline().strip() == "locked"
        with pytest.raises(RuntimeError, match="another API"), api_instance_lock(tmp_path):
            pass
    finally:
        child.stdin.write("\n")
        child.stdin.flush()
        child.communicate(timeout=10)
    assert child.returncode == 0
    with api_instance_lock(tmp_path):
        pass


@pytest.mark.asyncio
async def test_lifespan_does_not_start_dispatcher_if_store_is_locked(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from forgeflow.api import main

    monkeypatch.setattr(
        main, "get_settings", lambda: SimpleNamespace(resource_store_path=lambda: tmp_path)
    )
    with api_instance_lock(tmp_path), pytest.raises(RuntimeError, match="another API"):
        async with main.lifespan(SimpleNamespace()):
            pytest.fail("a second API must not start")


@pytest.mark.asyncio
async def test_failed_startup_releases_the_store_lock(tmp_path, monkeypatch):
    from contextlib import asynccontextmanager
    from types import SimpleNamespace

    from forgeflow.api import main

    @asynccontextmanager
    async def failing_startup(app):
        raise ValueError("startup failed")
        yield

    monkeypatch.setattr(
        main, "get_settings", lambda: SimpleNamespace(resource_store_path=lambda: tmp_path)
    )
    monkeypatch.setattr(main, "_application_lifespan", failing_startup)
    with pytest.raises(ValueError, match="startup failed"):
        async with main.lifespan(SimpleNamespace()):
            pass
    with api_instance_lock(tmp_path):
        pass
