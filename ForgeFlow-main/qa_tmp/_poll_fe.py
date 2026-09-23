import sys
import time
import urllib.error
import urllib.request

ok = False
for i in range(30):
    try:
        with urllib.request.urlopen("http://127.0.0.1:5173/", timeout=2) as r:
            print("frontend", r.status, flush=True)
            ok = True
            break
    except urllib.error.HTTPError as e:
        print("try", i, "HTTPError", e.code, flush=True)
    except Exception as e:  # noqa: BLE001
        print("try", i, type(e).__name__, str(e)[:60], flush=True)
    time.sleep(1)
print("FE_REACHABLE" if ok else "FE_UNREACHABLE", flush=True)
sys.exit(0 if ok else 1)
