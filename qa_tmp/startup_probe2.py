"""Startup probe v2: test Windows SelectorEventLoop + memory backend on real PG."""
import asyncio, os, sys, traceback

ROOT = r"D:\Agent项目\企业级 Multi-Agent 智能工作与技能资产平台\ForgeFlow-main"
sys.path.insert(0, ROOT)
os.chdir(ROOT)

want_selector = "selector" in sys.argv
backend = "postgres" if "postgres" in sys.argv else "memory"

if want_selector:
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

os.environ["STORAGE_BACKEND"] = backend
os.environ["LLM_PROVIDER"] = "mock"
os.environ["EMBEDDING_PROVIDER"] = "mock"
os.environ["DEV_LOGIN_ENABLED"] = "true"
os.environ["DEV_LOGIN_PASSWORD"] = "forgeflow-dev"
os.environ["TRACING_PROVIDER"] = "none"
os.environ["DOCS_ENABLED"] = "true"
os.environ["POSTGRES_URL"] = "postgresql+asyncpg://forgeflow:forgeflow@127.0.0.1:5433/forgeflow"
os.environ["POSTGRES_SYNC_URL"] = "postgresql+psycopg://forgeflow:forgeflow@127.0.0.1:5433/forgeflow"

from forgeflow.api.main import app


async def main():
    try:
        async with app.router.lifespan_context(app):
            print(f"LIFESPAN_OK selector={want_selector} backend={backend} pool={getattr(app.state, 'pool', None) is not None} graphs={list(getattr(app.state, 'graphs', {}).keys())}")
    except Exception as e:
        print(f"LIFESPAN_FAIL selector={want_selector} backend={backend} {type(e).__name__}: {e}")


asyncio.run(main())
