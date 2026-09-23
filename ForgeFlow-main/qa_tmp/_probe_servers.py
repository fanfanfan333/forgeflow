import json
import urllib.error
import urllib.request


def probe(name, url):
    try:
        with urllib.request.urlopen(url, timeout=4) as r:
            print(name, r.status, r.read(120).decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        print(name, "HTTPError", e.code)
    except Exception as e:  # noqa: BLE001
        print(name, "ERR", type(e).__name__, str(e)[:80])


probe("backend8010", "http://127.0.0.1:8010/health")
probe("frontend5173", "http://127.0.0.1:5173/")
# cost endpoints direct (independent of UI)
probe("cost_board", "http://127.0.0.1:8010/cost/board")
probe("cost_savings", "http://127.0.0.1:8010/cost/savings")
print(json.dumps({"ok": True}))
