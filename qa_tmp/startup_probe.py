"""Startup probe: does the lifespan boot under (a) unreachable PG, (b) real PG?"""
import asyncio, os, sys, traceback

ROOT = r"D:\Agent项目\企业级 Multi-Agent 智能工作与技能资产平台\ForgeFlow-main"
sys.path.insert(0, ROOT)
os.chdir(ROOT)

mode = sys.argv[1] if len(sys.argv) > 1 else "unreachable"
os.environ["STORAGE_BACKEND"] = "memory"
os.environ["LLM_PROVIDER"] = "mock"
os.environ["EMBEDDING_PROVIDER"] = "mock"
os.environ["DEV_LOGIN_ENABLED"] = "true"
os.environ["DEV_LOGIN_PASSWORD"] = "forgeflow-dev"
os.environ["TRACING_PROVIDER"] = "none"
os.environ["DOCS_ENABLED"] = "true"
if mode == "unreachable":
    os.environ["POSTGRES_URL"] = "postgresql+asyncpg://forgeflow:forgeflow@127.0.0.1:5999/forgeflow"
    os.environ["POSTGRES_SYNC_URL"] = "postgresql+psycopg://forgeflow:forgeflow@127.0.0.1:5999/forgeflow"
else:
    os.environ["POSTGRES_URL"] = "postgresql+asyncpg://forgeflow:forgeflow@127.0.0.1:5433/forgeflow"
    os.environ["POSTGRES_SYNC_URL"] = "postgresql+psycopg://forgeflow:forgeflow@127.0.0.1:5433/forgeflow"

from forgeflow.api.main import app


async def main():
    try:
        async with app.router.lifespan_context(app):
            print(f"LIFESPAN_OK mode={mode} pool={getattr(app.state, 'pool', None) is not None}")
    except Exception as e:
        print(f"LIFESPAN_FAIL mode={mode} {type(e).__name__}: {e}")
        traceback.print_exc()


asyncio.run(main())
