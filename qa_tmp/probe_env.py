"""WS-C environment probe for the runbook: PG 5433 + Ollama 11434 + alembic head."""
import asyncio, json, sys, os

ROOT = r"D:\Agent项目\企业级 Multi-Agent 智能工作与技能资产平台\ForgeFlow-main"
sys.path.insert(0, ROOT)
os.chdir(ROOT)

OUT = []


def log(*a):
    OUT.append(" ".join(str(x) for x in a))


async def probe_pg():
    import asyncpg
    # 1) correct creds -> project db
    for label, dsn, db in [
        ("forgeflow@5433/forgeflow", "postgresql://forgeflow:forgeflow@127.0.0.1:5433/forgeflow", "forgeflow"),
        ("postgres@5433/postgres (expected FAIL)", "postgresql://postgres:postgres@127.0.0.1:5433/postgres", "postgres"),
    ]:
        try:
            conn = await asyncpg.connect(dsn=dsn, timeout=8)
            if "forgeflow" in label:
                ver = await conn.fetchval("SELECT version()")
                n = await conn.fetchval(
                    "SELECT count(*) FROM information_schema.tables WHERE table_schema='public'"
                )
                alembic = None
                try:
                    alembic = await conn.fetchval("SELECT version_num FROM alembic_version")
                except Exception as e:
                    alembic = f"<no alembic_version: {e}>"
                hasvec = await conn.fetchval(
                    "SELECT count(*) FROM pg_extension WHERE extname='vector'"
                )
                log(f"PG OK [{label}] tables={n} alembic={alembic} pgvector={hasvec}")
                log(f"PG version: {ver.split(',')[0]}")
                # a couple of hub tables
                for t in ("cost_budgets", "skill_listings", "experiences", "skills", "skill_ratings", "context_build_stats"):
                    ex = await conn.fetchval(
                        "SELECT count(*) FROM information_schema.tables WHERE table_schema='public' AND table_name=$1", t
                    )
                    log(f"  table {t}: exists={bool(ex)}")
                # check experiences has 011 columns
                cols = await conn.fetch(
                    "SELECT column_name FROM information_schema.columns WHERE table_name='experiences' AND column_name IN ('conflict_with','merged_from','dedup_key','confidence')"
                )
                log(f"  experiences 011 cols: {sorted(r['column_name'] for r in cols)}")
            else:
                log(f"PG UNEXPECTEDLY OK [{label}]")
            await conn.close()
        except Exception as e:
            log(f"PG ERROR [{label}] {type(e).__name__}: {e}")

    # 2) maintenance db /postgres with forgeflow creds
    try:
        conn = await asyncpg.connect(dsn="postgresql://forgeflow:forgeflow@127.0.0.1:5433/postgres", timeout=8)
        n = await conn.fetchval(
            "SELECT count(*) FROM information_schema.tables WHERE table_schema='public'"
        )
        log(f"PG forgeflow@/postgres tables={n}")
        await conn.close()
    except Exception as e:
        log(f"PG ERROR [forgeflow@/postgres] {type(e).__name__}: {e}")


def probe_ollama():
    import urllib.request
    # disable proxies (sandbox HTTP_PROXY breaks localhost)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open("http://127.0.0.1:11434/api/tags", timeout=8) as r:
            data = json.loads(r.read().decode())
        models = [m.get("name") for m in data.get("models", [])]
        log(f"OLLAMA OK models={models}")
    except Exception as e:
        log(f"OLLAMA ERROR {type(e).__name__}: {e}")


def probe_paths():
    for p in [
        "frontend/package.json",
        "frontend/vite.config.ts",
        "frontend/src/App.tsx",
        "alembic.ini",
        "requirements.txt",
    ]:
        log(f"path {p}: exists={os.path.exists(os.path.join(ROOT, p))}")


probe_paths()
asyncio.run(probe_pg())
probe_ollama()

with open(os.path.join(ROOT, "..", "qa_tmp", "env_result.txt"), "w", encoding="utf-8") as f:
    f.write("\n".join(OUT))
print("\n".join(OUT))
