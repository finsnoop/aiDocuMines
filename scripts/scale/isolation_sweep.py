"""
Cross-organisation isolation sweep over every GET API route.

Data from `manage.py seed_scale`: every organisation's names and text carry a unique marker (ZQALPHA...).
  A. every route without path parameters, as an Admin, Manager, Auditor and member of each organisation:
     the response must not contain another organisation's marker;
  B. every route with path parameters, filled with IDs of ANOTHER organisation's objects:
     the response must not be 2xx with that organisation's data (expected 403/404).
Also records 5xx errors and response times per route.

    python isolation_sweep.py --base http://127.0.0.1:8099 --tokens tokens.json --routes routes.json \
        --ids ids.txt --out sweep.json
tokens.json / routes.json / ids.txt: see docs in seed_scale and the route/ID export commands.
"""
import argparse
import concurrent.futures as cf
import json
import random
import re
import time
import urllib.error
import urllib.request
from collections import Counter, defaultdict

NEVER = ("logout", "revoke")            # would end the sweep's own sessions
PARAM_KIND = {                          # path parameter -> which foreign object ID to use
    "file_id": "file", "user_id": "user", "matter_id": "matter", "org_id": "org", "client_id": "org",
    "task_id": "run", "job_id": "run", "folder_id": "folder", "link_id": "link",
}


def route_to_path(route: str, values: dict) -> str | None:
    """Turn a Django route ('api/v1/x/<int:pk>/', regex or not) into a concrete path."""
    route = route.replace("^", "").replace("$", "").replace("\\.", ".").replace("\\Z", "")
    route = re.sub(r"\(\?P<format>[^)]*\)", "", route)          # drop DRF .json/.api format suffix routes
    route = re.sub(r"\.?<(?:\w+:)?format>", "", route)

    def sub(m):
        name = m.group(1) or m.group(2)
        return str(values.get(name, values.get("pk")))
    path = re.sub(r"<(?:\w+:)?(\w+)>|\(\?P<(\w+)>[^)]*\)", sub, route)
    if "(" in path or "[" in path or "None" in path:
        return None
    return "/" + path.lstrip("/")


def call(base, path, token):
    req = urllib.request.Request(base + path, headers={"Authorization": f"Bearer {token}", "Accept": "application/json"})
    t0 = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            status, body = r.status, r.read(2_000_000)
    except urllib.error.HTTPError as e:
        status, body = e.code, e.read(200_000)
    except Exception as e:  # timeouts, resets
        status, body = "ERR", str(e).encode()
    return status, body.decode("utf-8", "replace"), (time.perf_counter() - t0) * 1000


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", required=True)
    ap.add_argument("--tokens", required=True)
    ap.add_argument("--routes", required=True)
    ap.add_argument("--ids", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--threads", type=int, default=8)
    a = ap.parse_args()

    users = json.load(open(a.tokens, encoding="utf-8"))
    markers = sorted({u["marker"] for u in users})
    ids = defaultdict(dict)                                      # org name -> kind -> id
    for line in open(a.ids, encoding="utf-8"):
        parts = line.strip().split("|")
        if len(parts) == 3:
            ids[parts[0]][parts[1]] = parts[2]
    rng = random.Random(7)
    identities = []
    for m in markers:
        for role in ("Admin", "Manager", "Auditor", "Client"):
            pick = [u for u in users if u["marker"] == m and u["role"] == role]
            if pick:
                identities.append(rng.choice(pick))

    routes = [r for r in json.load(open(a.routes, encoding="utf-8"))
              if "GET" in r["methods"] and not any(w in r["route"].lower() for w in NEVER)
              and "format" not in r["route"]]
    plain = [r for r in routes if "<" not in r["route"] and "(?P" not in r["route"]]
    with_params = [r for r in routes if r not in plain]

    jobs = []
    for who in identities:
        for r in plain:
            jobs.append(("A", who, r, route_to_path(r["route"], {}), None))
        foreign_org = next(o for o in ids if o != who["organisation"])
        foreign = ids[foreign_org]
        foreign_marker = next(u["marker"] for u in users if u["organisation"] == foreign_org)
        for r in with_params:
            names = re.findall(r"<(?:\w+:)?(\w+)>|\(\?P<(\w+)>", r["route"])
            names = [x or y for x, y in names]
            # pk routes are unknown models: probe with each foreign object kind that exists
            kinds = [PARAM_KIND.get(n) for n in names]
            candidates = ["file", "folder", "link", "user", "matter", "run"] if "pk" in names else [None]
            for kind in candidates:
                values = {}
                for n, k in zip(names, kinds):
                    values[n] = foreign.get(kind or k or "file", foreign.get("file"))
                path = route_to_path(r["route"], values)
                if path:
                    jobs.append(("B", who, r, path, foreign_marker))

    def run(job):
        phase, who, r, path, foreign_marker = job
        if not path:
            return None
        status, body, ms = call(a.base, path, who["token"])
        others = [m for m in markers if m != who["marker"] and m in body]
        leak = bool(others) and isinstance(status, int) and status < 300
        return {"phase": phase, "org": who["marker"], "role": who["role"], "route": r["route"], "view": r["view"],
                "path": path, "status": status, "ms": round(ms), "bytes": len(body),
                "foreign_markers": others, "leak": leak,
                "foreign_object_2xx": phase == "B" and isinstance(status, int) and status < 300,
                "snippet": body[:200] if (leak or status == "ERR" or (isinstance(status, int) and status >= 500)) else ""}

    t0 = time.time()
    with cf.ThreadPoolExecutor(a.threads) as ex:
        results = [x for x in ex.map(run, jobs) if x]
    json.dump(results, open(a.out, "w", encoding="utf-8"), indent=0)

    leaks = [x for x in results if x["leak"]]
    errors = [x for x in results if x["status"] == "ERR" or (isinstance(x["status"], int) and x["status"] >= 500)]
    b2xx = [x for x in results if x["foreign_object_2xx"] and not x["leak"]]
    print(f"{len(results)} calls in {time.time() - t0:.0f}s as {len(identities)} identities "
          f"({len(plain)} plain routes, {len(with_params)} routes with IDs)")
    print("status:", dict(Counter(str(x["status"]) for x in results)))
    print(f"LEAKS (another organisation's data in a 2xx response): {len(leaks)} calls on "
          f"{len({x['route'] for x in leaks})} routes")
    for route, n in Counter(x["route"] for x in leaks).most_common(40):
        ex_ = next(x for x in leaks if x["route"] == route)
        print(f"   {n:4}x {route}  ({ex_['role']} {ex_['org']} saw {','.join(ex_['foreign_markers'])})")
    print(f"foreign object IDs answered 2xx without visible foreign data (review): "
          f"{len({x['route'] for x in b2xx})} routes")
    for route in sorted({x["route"] for x in b2xx})[:60]:
        print("   ", route)
    print(f"server errors: {len(errors)} calls on {len({x['route'] for x in errors})} routes")
    for route, n in Counter(x["route"] for x in errors).most_common(40):
        print(f"   {n:4}x {route}")
    slow = defaultdict(list)
    for x in results:
        if x["phase"] == "A" and isinstance(x["status"], int) and x["status"] < 300:
            slow[x["route"]].append(x["ms"])
    worst = sorted(((max(v), r) for r, v in slow.items()), reverse=True)[:20]
    print("slowest routes (max ms, single user, no load):")
    for ms, r in worst:
        print(f"   {ms:6} ms  {r}")


if __name__ == "__main__":
    main()
