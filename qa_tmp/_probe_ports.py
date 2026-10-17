import urllib.request, json
for name, url in [("backend8010-health","http://127.0.0.1:8010/health"),
                  ("frontend5173","http://127.0.0.1:5173/"),
                  ("frontend4173","http://127.0.0.1:4173/")]:
    try:
        with urllib.request.urlopen(url, timeout=5) as r:
            print(name, r.status, r.read(120)[:120])
    except Exception as e:
        print(name, "ERR", type(e).__name__, e)
