import socket, sys, os
os.environ["NO_PROXY"] = "localhost,127.0.0.1"
os.environ["no_proxy"] = "localhost,127.0.0.1"
sys.path.insert(0, r"D:\Agent项目\企业级 Multi-Agent 智能工作与技能资产平台\ForgeFlow-main")

# 1) TCP reachability
try:
    s = socket.create_connection(("localhost", 5433), timeout=4)
    print("TCP localhost:5433 OPEN")
    s.close()
except Exception as e:
    print("TCP localhost:5433 FAILED:", repr(e))

# 2) psycopg connect
try:
    import psycopg
    with psycopg.connect("postgresql://forgeflow:forgeflow@localhost:5433/forgeflow", connect_timeout=5) as conn:
        with conn.cursor() as cur:
            cur.execute("select version()")
            print("PSYCOPG OK:", cur.fetchone()[0][:70])
            cur.execute("select current_database(), current_user")
            print("DB/USER:", cur.fetchone())
            cur.execute("select table_name from information_schema.tables where table_schema='public' order by 1")
            tabs = [r[0] for r in cur.fetchall()]
            print("TABLES(%d):" % len(tabs), tabs)
            cur.execute("select version_num from alembic_version")
            print("ALEMBIC VERSION:", cur.fetchall())
except Exception as e:
    print("PSYCOPG FAILED:", repr(e))
