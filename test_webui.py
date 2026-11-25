"""
test_webui.py — smoke test of the Web UI (webui/app.py) without a browser.

Uses Flask's test client against the real app, rules and config:
  1. every page and API answers 401 / redirects to /login without a session
  2. a wrong password is refused, the right one opens a session
  3. the dashboard renders and /api/health and /api/rules load every shipped
     rule with no load errors
  4. the rule tester (/api/test) parses each rule's golden input.log - the
     same path a contributor uses in the browser
Nothing is written: no rule/config saves, no engine start/stop.

Needs Flask (webui/requirements-ui.txt). No Kafka/Redis.
Run:  python3 test_webui.py   (exit 0 = pass)
"""
import importlib.util
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SAMPLES_DIR = os.path.join(HERE, "tests", "samples")

# Credentials for this process only; no login file is created.
os.environ["SOC_UI_USER"] = "ci"
os.environ["SOC_UI_PASSWORD"] = "ci-smoke-test"
os.environ.pop("SOC_UI_NO_AUTH", None)

passed = failed = 0


def check(name, ok, detail=""):
    global passed, failed
    if ok:
        passed += 1
        print(f"[PASS] {name}")
    else:
        failed += 1
        print(f"[FAIL] {name}" + (f": {detail}" if detail else ""))


def load_app():
    sys.path.insert(0, os.path.join(HERE, "webui"))
    spec = importlib.util.spec_from_file_location(
        "webui_app", os.path.join(HERE, "webui", "app.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.app


def main():
    app = load_app()
    app.config["TESTING"] = True
    c = app.test_client()

    r = c.get("/api/rules")
    check("API refuses without login", r.status_code == 401, r.status_code)
    r = c.get("/")
    check("pages redirect to /login without login",
          r.status_code == 302 and "/login" in r.headers.get("Location", ""),
          r.status_code)
    check("login page renders", c.get("/login").status_code == 200)

    r = c.post("/api/login", json={"username": "ci", "password": "wrong"})
    check("wrong password refused", r.status_code != 200 or
          not (r.get_json() or {}).get("ok"), r.status_code)
    r = c.post("/api/login", json={"username": "ci",
                                   "password": "ci-smoke-test"})
    check("login succeeds", r.status_code == 200 and r.get_json().get("ok"),
          r.status_code)

    check("dashboard renders", c.get("/").status_code == 200)

    r = c.get("/api/health")
    health = r.get_json() or {}
    check("/api/health loads", r.status_code == 200 and health.get("ok"),
          r.status_code)
    check("/api/health: no rule load errors", not health.get("load_errors"),
          health.get("load_errors"))

    r = c.get("/api/rules")
    data = r.get_json() or {}
    names = {x["name"] for x in data.get("rules", [])}
    check("/api/rules lists rules", r.status_code == 200 and names,
          r.status_code)
    check("/api/rules: no load errors", not data.get("errors"),
          data.get("errors"))

    for name in sorted(os.listdir(SAMPLES_DIR)):
        path = os.path.join(SAMPLES_DIR, name, "input.log")
        if name not in names or not os.path.isfile(path):
            continue
        with open(path, encoding="utf-8") as f:
            text = f.read()
        r = c.post("/api/test", json={"parser": name, "text": text})
        body = r.get_json() or {}
        check(f"rule tester runs {name}", r.status_code == 200 and
              "error" not in body, body.get("error", r.status_code))

    print("-" * 60)
    print(f"{passed}/{passed + failed} passed")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
