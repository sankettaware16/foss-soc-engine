#!/usr/bin/env python3
"""
FOSS SOC Engine - Web UI
========================

A single, dependency-light Flask application that puts every *local* capability
of the engine behind a browser UI, so an operator never has to run a python
file by hand:

  * Test Log   -> paste a line or upload a log file, pick a parser (or AUTO),
                  see the parsed ECS JSON + a parse/no-match summary.
  * Rules      -> list / view / edit / create / delete parser rules (YAML),
                  with live ECS field checking and per-strategy starter templates.
  * Config     -> edit config.yaml in the browser and run the static validator
                  (the same checks as test_config.py).
  * ECS Helper -> search ECS fields by concept and autocorrect a field name
                  (the same brain as ecs_helper.py).
  * Preflight  -> run the live readiness checks (Kafka / Redis / TCP), degrading
                  gracefully with a clear notice when those libs/services are
                  not present on the machine.

Design goals: runs on **Flask + PyYAML only** (redis / geoip2 / kafka / orjson
are all optional and degrade gracefully), serves all assets locally (works fully
offline, no CDN), and is PyInstaller-friendly so it can ship as a single .exe.
"""

import os
import sys
import io
import re
import json
import glob
import time
import hmac
import shutil
import socket
import hashlib
import secrets
import platform
import subprocess
import contextlib

# --------------------------------------------------------------------------- #
# Path handling. Works in three modes:
#   * `python webui/app.py`         (developer / from the repo)
#   * frozen PyInstaller .exe       (DATA next to the exe, code bundled inside)
# --------------------------------------------------------------------------- #
FROZEN = getattr(sys, "frozen", False)

if FROZEN:
    # Bundled templates/static live in the temp extraction dir; the editable
    # rules/ + config.yaml live next to the executable so operators can change
    # them without rebuilding.
    BUNDLE_DIR = sys._MEIPASS  # type: ignore[attr-defined]
    DATA_ROOT = os.path.dirname(sys.executable)
else:
    BUNDLE_DIR = os.path.dirname(os.path.abspath(__file__))          # webui/
    DATA_ROOT = os.path.dirname(BUNDLE_DIR)                          # repo root
    if DATA_ROOT not in sys.path:
        sys.path.insert(0, DATA_ROOT)

TEMPLATES_DIR = os.path.join(BUNDLE_DIR, "templates")
STATIC_DIR = os.path.join(BUNDLE_DIR, "static")

# --------------------------------------------------------------------------- #
# Imports from the engine. These must succeed on Flask + PyYAML alone; the heavy
# optional deps (redis, geoip2) were made lazy in core/engine.py + utils/geoip.py
# --------------------------------------------------------------------------- #
import yaml  # noqa: E402
from flask import (  # noqa: E402
    Flask, request, jsonify, render_template, session, redirect,
)

from core.engine import UniversalEngine  # noqa: E402
from core.schema import LogInput  # noqa: E402
from core import ecs_schema  # noqa: E402
import test_config as tc  # noqa: E402

# Optional-availability probes (used by the dashboard banners).
try:
    import core.engine as _engine_mod
    REDIS_OK = _engine_mod.r is not None
except Exception:
    REDIS_OK = False

try:
    import utils.fastjson as _fj
    ORJSON_OK = getattr(_fj, "HAVE_ORJSON", False)
except Exception:
    ORJSON_OK = False

try:
    import geoip2.database  # noqa: F401
    GEOIP_LIB_OK = True
except Exception:
    GEOIP_LIB_OK = False

try:
    import kafka  # noqa: F401
    KAFKA_LIB_OK = True
except Exception:
    KAFKA_LIB_OK = False

CONFIG_PATH = os.path.join(DATA_ROOT, "config.yaml")
# The running engine (main.py) writes its heartbeat + stats into <root>/logs.
# Override with SOC_LOG_DIR if the UI and engine live in different folders.
LOG_DIR = os.environ.get("SOC_LOG_DIR") or os.path.join(DATA_ROOT, "logs")
ENGINE_PID_PATH = os.path.join(LOG_DIR, "engine.pid")

# --------------------------------------------------------------------------- #
# Authentication credentials.
#   Priority:
#     1. TLSOCDocker ELK  .env  ->  log in with the SAME elastic user + password
#        (so the console shares the ELK login; the default is then NOT used).
#     2. SOC_UI_USER / SOC_UI_PASSWORD env vars.
#     3. Built-in default (admin / admin) — only when no .env is found.
#   Disable auth entirely (local dev only) with SOC_UI_NO_AUTH=1.
# --------------------------------------------------------------------------- #
# No absolute default path is assumed: point at your ELK .env explicitly via
# SOC_ENV_FILE or config.yaml `auth.env_file`. A sibling TLSOCDockerDeploy/
# checkout and <root>/.env are still searched automatically (see below).
DEFAULT_ENV_PATHS = []


def _parse_env_file(path):
    data = {}
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                # split on the FIRST '=' only, so values that contain '='
                # (e.g. SOME_PASSWORD==abc=123=) are read correctly.
                k, _, v = line.partition("=")
                k = k.strip()
                v = v.strip().strip('"').strip("'")
                if k:
                    data[k] = v
    except Exception:
        return None
    return data


def _find_elk_env():
    """Return (path, parsed) for the first .env that has an ELASTIC_PASSWORD,
    or (None, None).

    A path the operator configured EXPLICITLY (SOC_ENV_FILE or config.yaml)
    that turns out to be missing/unusable is reported loudly at startup —
    a typo here must never fail silently into the generated-password login."""
    explicit = []   # (where-it-was-configured, path)
    if os.environ.get("SOC_ENV_FILE"):
        explicit.append(("SOC_ENV_FILE", os.environ["SOC_ENV_FILE"]))
    # optional config.yaml auth.env_file (read directly - load_config() is
    # defined further down and this runs at import time).
    cfg = {}
    try:
        if os.path.exists(CONFIG_PATH):
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                cfg = yaml.safe_load(f) or {}
    except Exception:
        cfg = {}
    auth_cfg = cfg.get("auth") or {}
    if auth_cfg.get("env_file"):
        explicit.append(("config.yaml auth.env_file", auth_cfg["env_file"]))
    # Forgiveness: a top-level `env_file:` (the natural indentation slip when
    # adding the auth block by hand) is accepted too.
    if cfg.get("env_file"):
        explicit.append(("config.yaml env_file", cfg["env_file"]))

    candidates = [p for _, p in explicit]
    candidates += DEFAULT_ENV_PATHS
    candidates.append(os.path.join(os.path.dirname(DATA_ROOT), "TLSOCDockerDeploy", ".env"))
    candidates.append(os.path.join(DATA_ROOT, ".env"))
    for p in candidates:
        if p and os.path.isfile(p):
            env = _parse_env_file(p)
            if env and env.get("ELASTIC_PASSWORD"):
                return p, env
    for where, p in explicit:
        why = ("file not found" if not os.path.isfile(str(p))
               else "no ELASTIC_PASSWORD in it")
        print(f"[auth] WARNING: {where} points at {p} but it is unusable "
              f"({why}) - falling back to the next credential source")
    return None, None


# Local auth store: replaces the old well-known admin/admin default (audit
# P1-6). On first run a random password is GENERATED, stored salted+hashed
# here, and printed ONCE to the console. Delete the file to regenerate.
AUTH_FILE = os.path.join(DATA_ROOT, ".soc-ui-auth.json")


def _load_or_create_local_auth():
    """Return ({username, salt, password_sha256[, _new_password]}, created)."""
    try:
        with open(AUTH_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if data.get("password_sha256") and data.get("salt"):
            return data, False
    except Exception:
        pass
    pw = secrets.token_urlsafe(9)
    salt = secrets.token_hex(16)
    data = {
        "username": "admin",
        "salt": salt,
        "password_sha256": hashlib.sha256((salt + pw).encode()).hexdigest(),
    }
    try:
        with open(AUTH_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f)
    except Exception:
        pass  # not persistable (read-only dir): still valid for this run
    data["_new_password"] = pw
    return data, True


def resolve_auth():
    """Credential source for the UI login, in priority order. Returns a dict:
    {mode: 'plain'|'hash', user, source, ...} — 'plain' carries `password`,
    'hash' carries `salt` + `digest` (nothing recoverable on disk)."""
    path, env = _find_elk_env()
    if env and env.get("ELASTIC_PASSWORD"):
        user = env.get("ELASTIC_USERNAME") or "elastic"
        return {"mode": "plain", "user": user,
                "password": env["ELASTIC_PASSWORD"],
                "source": f"elk-env:{path}"}
    u, p = os.environ.get("SOC_UI_USER"), os.environ.get("SOC_UI_PASSWORD")
    if u and p:
        return {"mode": "plain", "user": u, "password": p,
                "source": "env-vars"}
    data, created = _load_or_create_local_auth()
    return {"mode": "hash", "user": data["username"], "salt": data["salt"],
            "digest": data["password_sha256"], "source": "local-file",
            "new_password": data.get("_new_password") if created else None}


app = Flask(__name__, template_folder=TEMPLATES_DIR, static_folder=STATIC_DIR)
app.config["MAX_CONTENT_LENGTH"] = 200 * 1024 * 1024  # 200 MB upload cap
app.secret_key = os.environ.get("SOC_UI_SECRET") or os.urandom(32)
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"

AUTH_DISABLED = os.environ.get("SOC_UI_NO_AUTH") == "1"
AUTH = resolve_auth()
AUTH_USER, AUTH_SOURCE = AUTH["user"], AUTH["source"]
AUTH_FROM_ELK = AUTH_SOURCE.startswith("elk-env:")
_ALLOW_UNAUTH = {"/login", "/api/login"}

# Loud, unmissable startup notices — silent-default and silent-no-auth are
# exactly how consoles end up exposed (audit P1-6).
if AUTH_DISABLED:
    print("\n" + "!" * 74)
    print("!!  SOC_UI_NO_AUTH=1 - AUTHENTICATION IS DISABLED.")
    print("!!  Anyone who can reach this port fully controls rules & config.")
    print("!!  Only ever use this on localhost or an isolated Docker network.")
    print("!" * 74 + "\n")
elif AUTH.get("new_password"):
    print("\n" + "=" * 74)
    print("  FIRST RUN - a login was generated for this console:")
    print(f"      username: {AUTH_USER}")
    print(f"      password: {AUTH['new_password']}")
    print(f"  (stored salted+hashed in {AUTH_FILE};")
    print("   delete that file to generate a new password, or set")
    print("   SOC_UI_USER / SOC_UI_PASSWORD to choose your own.)")
    print("=" * 74 + "\n")
elif AUTH_SOURCE == "local-file":
    print(f"[auth] using the generated local login (user '{AUTH_USER}'; "
          f"reset: delete {AUTH_FILE})")
else:
    print(f"[auth] credentials from {AUTH_SOURCE} (user '{AUTH_USER}')")


def check_credentials(username, password):
    ok_u = hmac.compare_digest(str(username), AUTH_USER)
    if AUTH["mode"] == "plain":
        ok_p = hmac.compare_digest(str(password), AUTH["password"])
    else:
        digest = hashlib.sha256(
            (AUTH["salt"] + str(password)).encode()).hexdigest()
        ok_p = hmac.compare_digest(digest, AUTH["digest"])
    return ok_u and ok_p


@app.before_request
def _require_auth():
    if AUTH_DISABLED:
        return None
    p = request.path
    if p.startswith("/static/") or p in _ALLOW_UNAUTH or p == "/favicon.ico":
        return None
    if session.get("auth"):
        return None
    if p.startswith("/api/"):
        return jsonify({"error": "authentication required", "login": "/login"}), 401
    return redirect("/login")


@app.route("/login")
def login_page():
    if session.get("auth") or AUTH_DISABLED:
        return redirect("/")
    if AUTH_FROM_ELK:
        hint = "Sign in with your ELK / Elastic credentials."
    elif AUTH_SOURCE == "env-vars":
        hint = "Sign in with the SOC_UI_USER / SOC_UI_PASSWORD you configured."
    else:
        hint = ("Sign in as <b>admin</b> with the password printed in the "
                "console window on first start (under systemd: "
                "<code>journalctl -u foss-soc-ui | grep password:</code>). "
                "Forgot it? Delete <code>.soc-ui-auth.json</code> next to the "
                "app and restart. Prefer your ELK/Elastic login? Set "
                "<code>auth.env_file</code> in config.yaml — see the README.")
    return render_template("login.html", hint=hint)


@app.route("/api/login", methods=["POST"])
def api_login():
    body = request.get_json(force=True, silent=True) or {}
    if check_credentials(body.get("username", ""), body.get("password", "")):
        session["auth"] = True
        session["user"] = AUTH_USER
        return jsonify({"ok": True, "user": AUTH_USER})
    time.sleep(0.6)  # gently slow brute-force attempts
    return jsonify({"error": "Invalid username or password"}), 401


@app.route("/api/logout", methods=["POST"])
def api_logout():
    session.clear()
    return jsonify({"ok": True})


@app.route("/api/whoami")
def api_whoami():
    return jsonify({
        "user": session.get("user") or (AUTH_USER if AUTH_DISABLED else None),
        "auth_disabled": AUTH_DISABLED,
        "source": "elk" if AUTH_FROM_ELK else ("env" if AUTH_SOURCE == "env-vars" else "default"),
    })


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #
def load_config():
    """Read config.yaml from DATA_ROOT. Returns {} on any failure (UI shows it)."""
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except Exception:
        return {}


def rules_dir():
    cfg = load_config()
    rel = (cfg.get("paths") or {}).get("rules_dir", "rules/")
    return rel if os.path.isabs(rel) else os.path.join(DATA_ROOT, rel)


def program_map():
    return load_config().get("program_mapping") or {}


def build_engines():
    """Compile every rules/*.yaml into a UniversalEngine. Returns
    (engines, meta, errors) with no background watcher thread."""
    # Honor config.yaml `redis:` so testing a stateful rule from the UI talks
    # to the same Redis as the engine (defaults to localhost when absent).
    try:
        from core.engine import configure_redis
        configure_redis(load_config().get("redis"))
    except Exception:
        pass
    engines, meta, errors = {}, {}, []
    rdir = rules_dir()
    if not os.path.isdir(rdir):
        return engines, meta, [{"file": "(rules dir)", "error": f"not found: {rdir}"}]
    for fname in sorted(os.listdir(rdir)):
        if not fname.endswith(".yaml"):
            continue
        path = os.path.join(rdir, fname)
        try:
            with open(path, "r", encoding="utf-8") as fh:
                cfg = yaml.safe_load(fh)
            if not isinstance(cfg, dict):
                errors.append({"file": fname, "error": "not a YAML mapping"})
                continue
            name = cfg.get("pattern_name", fname[:-5])
            eng = UniversalEngine(cfg)
            engines[name] = eng
            meta[name] = {
                "file": fname,
                "strategy": cfg.get("strategy", "stateless"),
                "fields": _count_fields(cfg),
                "disabled": getattr(eng, "disabled", False),
            }
        except Exception as e:  # noqa: BLE001
            errors.append({"file": fname, "error": str(e)})
    return engines, meta, errors


def _count_fields(cfg):
    n = len(cfg.get("mapping") or {})
    for p in cfg.get("patterns") or []:
        if isinstance(p, dict):
            n += len(p.get("mapping") or {})
    return n


def make_log_input(raw, program):
    env = json.dumps({"meta": {"source_program": program, "source_file": "webui"},
                      "raw": raw})
    return LogInput(env)


def _id_buffered(engine, raw):
    """Does a stateful rule recognise this as the start/middle of an event?"""
    if getattr(engine, "strategy", None) == "stateful":
        idr = getattr(engine, "id_regex", None)
        if idr is not None and idr.search(raw):
            return True
    return False


def run_test(lines, parser, sample_limit=5, max_events=300):
    """Mirror of test_file.py, returning a structured result for the browser."""
    engines, meta, load_errors = build_engines()
    pmap = program_map()
    auto = str(parser).strip().upper() == "AUTO"

    resolved = "AUTO"
    target_engine = None
    if not auto:
        target_engine = engines.get(parser)
        if target_engine is None:
            mapped = pmap.get(parser, parser)
            # program_mapping may map a source to a CHAIN (list of rules);
            # for a single-parser test, use the first rule that exists.
            if isinstance(mapped, list):
                mapped = next((m for m in mapped if m in engines),
                              mapped[0] if mapped else parser)
            target_engine = engines.get(mapped)
            resolved = mapped if target_engine is not None else parser
        else:
            resolved = parser
        if target_engine is None:
            return {
                "error": f"Parser '{parser}' not found. "
                         f"Available: {', '.join(sorted(engines)) or '(none)'}",
                "load_errors": load_errors,
            }

    stats = {"lines_read": 0, "nonempty": 0, "blank": 0, "parsed_lines": 0,
             "parsed_events": 0, "no_match": 0, "buffered": 0, "errors": 0}
    by_rule = {}
    samples = {"no_match": [], "buffered": [], "errors": []}
    events = []

    def add_sample(reason, ln, raw):
        if len(samples[reason]) < sample_limit:
            samples[reason].append({"line": ln, "raw": raw})

    for ln, line in enumerate(lines, start=1):
        stats["lines_read"] += 1
        raw = line.rstrip("\n")
        if not raw.strip():
            stats["blank"] += 1
            continue
        stats["nonempty"] += 1

        candidates = engines.items() if auto else [(resolved, target_engine)]
        matched = False
        buffered = False
        error_seen = False

        for ename, eng in candidates:
            try:
                result = eng.process(make_log_input(raw, ename))
            except Exception as e:  # noqa: BLE001
                error_seen = True
                if not auto:
                    add_sample("errors", ln, f"{raw}    ->  {e}")
                continue

            if result:
                matched = True
                items = result if isinstance(result, list) else [result]
                stats["parsed_lines"] += 1
                stats["parsed_events"] += len(items)
                by_rule[ename] = by_rule.get(ename, 0) + len(items)
                for ev in items:
                    if len(events) < max_events:
                        events.append({"line": ln, "rule": ename, "event": ev})
                break
            if _id_buffered(eng, raw):
                buffered = True

        if matched:
            continue
        if error_seen and auto:
            stats["errors"] += 1
            add_sample("errors", ln, raw)
        elif error_seen:
            stats["errors"] += 1
        elif buffered:
            stats["buffered"] += 1
            add_sample("buffered", ln, raw)
        else:
            stats["no_match"] += 1
            add_sample("no_match", ln, raw)

    stats["unparsed"] = stats["no_match"] + stats["buffered"] + stats["errors"]
    total = stats["parsed_lines"] + stats["unparsed"]
    stats["match_rate"] = round(100.0 * stats["parsed_lines"] / total, 1) if total else 0.0

    return {
        "parser": resolved,
        "stats": stats,
        "by_rule": by_rule,
        "samples": samples,
        "events": events,
        "events_truncated": stats["parsed_events"] > len(events),
        "load_errors": load_errors,
        "redis_ok": REDIS_OK,
    }


def parse_report_lines(text):
    """Turn captured `[LEVEL] message` / `=== section ===` output into objects."""
    out = []
    for ln in text.splitlines():
        s = ln.rstrip()
        if not s.strip():
            continue
        m = re.match(r"^\s*\[(\w+)\]\s*(.*)$", s)
        if m:
            out.append({"level": m.group(1).upper(), "message": m.group(2)})
        elif s.strip().startswith("==="):
            out.append({"level": "SECTION", "message": s.strip("= ").strip()})
        elif set(s.strip()) <= {"="}:
            continue
        else:
            out.append({"level": "TEXT", "message": s})
    return out


# ECS targets in a rule (same shape ecs_helper / test_config use).
def iter_rule_targets(rule):
    def block(mapping, static, where):
        if isinstance(mapping, dict):
            for src, tgt in mapping.items():
                yield tgt, f"{where} mapping[{src}]"
        if isinstance(static, dict):
            for k in static.keys():
                yield k, f"{where} static"
    yield from block(rule.get("mapping"), rule.get("static"), "rule")
    for i, p in enumerate(rule.get("patterns") or [], 1):
        if isinstance(p, dict):
            yield from block(p.get("mapping"), p.get("static"),
                             p.get("name") or f"pattern#{i}")


def ecs_check_rule(rule):
    problems, customs, ok = [], [], 0
    for field, loc in iter_rule_targets(rule):
        if not isinstance(field, str):
            continue
        status, sug = ecs_schema.classify(field)
        if status in ("alias", "typo"):
            problems.append({"field": field, "fix": sug, "loc": loc})
        elif status == "custom":
            customs.append({"field": field, "hint": sug, "loc": loc})
        else:
            ok += 1
    return {"ok": ok, "problems": problems, "customs": customs}


# --------------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------------- #
@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/health")
def api_health():
    engines, meta, errors = build_engines()
    cfg = load_config()
    return jsonify({
        "ok": True,
        "python": sys.version.split()[0],
        "frozen": FROZEN,
        "data_root": DATA_ROOT,
        "config_found": os.path.exists(CONFIG_PATH),
        "rules_count": len(engines),
        "rules": [{"name": n, **meta[n]} for n in sorted(meta)],
        "load_errors": errors,
        "strategies": _strategy_breakdown(meta),
        "program_mapping": cfg.get("program_mapping") or {},
        "capabilities": {
            "redis": REDIS_OK,
            "geoip": GEOIP_LIB_OK,
            "orjson": ORJSON_OK,
            "kafka": KAFKA_LIB_OK,
        },
    })


def _strategy_breakdown(meta):
    out = {}
    for m in meta.values():
        out[m["strategy"]] = out.get(m["strategy"], 0) + 1
    return out


# ---- Rules CRUD ----------------------------------------------------------- #
def _safe_rule_path(filename):
    fname = os.path.basename(filename or "")
    if not fname.endswith(".yaml"):
        return None, "filename must end with .yaml"
    if not re.match(r"^[A-Za-z0-9_.-]+$", fname):
        return None, "filename may only contain letters, numbers, _ . -"
    return os.path.join(rules_dir(), fname), None


@app.route("/api/rules")
def api_rules():
    _, meta, errors = build_engines()
    return jsonify({"rules": [{"name": n, **meta[n]} for n in sorted(meta)],
                    "errors": errors})


@app.route("/api/rules/<path:filename>")
def api_rule_get(filename):
    path, err = _safe_rule_path(filename)
    if err:
        return jsonify({"error": err}), 400
    if not os.path.exists(path):
        return jsonify({"error": "not found"}), 404
    with open(path, "r", encoding="utf-8") as f:
        return jsonify({"filename": os.path.basename(path), "content": f.read()})


@app.route("/api/rules/save", methods=["POST"])
def api_rule_save():
    body = request.get_json(force=True, silent=True) or {}
    path, err = _safe_rule_path(body.get("filename", ""))
    if err:
        return jsonify({"error": err}), 400
    content = body.get("content", "")
    try:
        parsed = yaml.safe_load(content)
    except Exception as e:  # noqa: BLE001
        return jsonify({"error": f"YAML syntax error: {e}"}), 400
    if not isinstance(parsed, dict):
        return jsonify({"error": "rule must be a YAML mapping (key: value)"}), 400

    os.makedirs(rules_dir(), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)

    ecs = ecs_check_rule(parsed)
    return jsonify({"saved": os.path.basename(path), "ecs": ecs})


@app.route("/api/rules/delete", methods=["POST"])
def api_rule_delete():
    body = request.get_json(force=True, silent=True) or {}
    path, err = _safe_rule_path(body.get("filename", ""))
    if err:
        return jsonify({"error": err}), 400
    if not os.path.exists(path):
        return jsonify({"error": "not found"}), 404
    os.remove(path)
    return jsonify({"deleted": os.path.basename(path)})


# ---- Test (paste text or upload file) ------------------------------------- #
@app.route("/api/test", methods=["POST"])
def api_test():
    parser = "AUTO"
    limit = 20000
    lines = []

    if request.files.get("file"):
        f = request.files["file"]
        parser = request.form.get("parser", "AUTO")
        try:
            limit = int(request.form.get("limit", 20000))
        except (TypeError, ValueError):
            limit = 20000
        raw = f.stream.read().decode("utf-8", "ignore")
        lines = raw.splitlines()
    else:
        body = request.get_json(force=True, silent=True) or {}
        parser = body.get("parser", "AUTO")
        text = body.get("text", "")
        try:
            limit = int(body.get("limit", 20000))
        except (TypeError, ValueError):
            limit = 20000
        lines = text.splitlines()

    truncated = False
    if limit and len(lines) > limit:
        lines = lines[:limit]
        truncated = True

    result = run_test(lines, parser)
    if "error" in result:
        return jsonify(result), 400
    result["input_truncated"] = truncated
    result["input_limit"] = limit
    return jsonify(result)


# ---- Config read / save / validate ---------------------------------------- #
@app.route("/api/config")
def api_config_get():
    if not os.path.exists(CONFIG_PATH):
        return jsonify({"content": "", "found": False})
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return jsonify({"content": f.read(), "found": True, "path": CONFIG_PATH})


@app.route("/api/config/save", methods=["POST"])
def api_config_save():
    body = request.get_json(force=True, silent=True) or {}
    content = body.get("content", "")
    try:
        yaml.safe_load(content)
    except Exception as e:  # noqa: BLE001
        return jsonify({"error": f"YAML syntax error: {e}"}), 400
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        f.write(content)
    return jsonify({"saved": True})


@app.route("/api/config/validate", methods=["POST"])
def api_config_validate():
    """Run the same static checks as test_config.py and return them structured."""
    buf = io.StringIO()
    summary = {"errors": 0, "warnings": 0}
    with contextlib.redirect_stdout(buf):
        cfg = tc.load_config(CONFIG_PATH)
        if cfg is None:
            cfg = {}
        e, w = tc.validate_config_shape(cfg); summary["errors"] += e; summary["warnings"] += w
        e, w = tc.validate_paths(DATA_ROOT, cfg); summary["errors"] += e; summary["warnings"] += w
        e, w, rules = tc.validate_rules(DATA_ROOT, cfg); summary["errors"] += e; summary["warnings"] += w
        e, w = tc.validate_program_mapping(cfg, rules); summary["errors"] += e; summary["warnings"] += w
        e, w = tc.validate_ecs_fields(rules); summary["errors"] += e; summary["warnings"] += w
    summary["passed"] = summary["errors"] == 0
    return jsonify({"summary": summary, "lines": parse_report_lines(buf.getvalue())})


# ---- ECS helper ----------------------------------------------------------- #
@app.route("/api/ecs/find")
def api_ecs_find():
    q = request.args.get("q", "").strip()
    if not q:
        return jsonify({"results": []})
    return jsonify({"results": ecs_schema.search(q, n=20)})


@app.route("/api/ecs/classify")
def api_ecs_classify():
    field = request.args.get("field", "").strip()
    if not field:
        return jsonify({"error": "field is required"}), 400
    status, sug = ecs_schema.classify(field)
    return jsonify({
        "field": field,
        "status": status,                 # ecs | alias | typo | custom
        "suggestion": sug,
        "suggestions": ecs_schema.suggest(field, n=6),
    })


@app.route("/api/ecs/check", methods=["POST"])
def api_ecs_check():
    body = request.get_json(force=True, silent=True) or {}
    content = body.get("content")
    if content is None and body.get("filename"):
        path, err = _safe_rule_path(body["filename"])
        if err:
            return jsonify({"error": err}), 400
        with open(path, "r", encoding="utf-8") as f:
            content = f.read()
    try:
        rule = yaml.safe_load(content or "")
    except Exception as e:  # noqa: BLE001
        return jsonify({"error": f"YAML syntax error: {e}"}), 400
    if not isinstance(rule, dict):
        return jsonify({"error": "rule must be a YAML mapping"}), 400
    return jsonify(ecs_check_rule(rule))


# ---- Preflight (live readiness) ------------------------------------------- #
@app.route("/api/preflight", methods=["POST"])
def api_preflight():
    import preflight as pf
    body = request.get_json(force=True, silent=True) or {}
    skip_live = bool(body.get("skip_live", False))
    try:
        timeout = float(body.get("timeout", 4.0))
    except (TypeError, ValueError):
        timeout = 4.0

    buf = io.StringIO()
    errors = 0
    with contextlib.redirect_stdout(buf):
        cfg = tc.load_config(CONFIG_PATH) or {}
        print("=== 1. Config structure ===")
        e, _ = tc.validate_config_shape(cfg); errors += e
        print("=== 2. Paths & GeoIP ===")
        tc.validate_paths(DATA_ROOT, cfg)
        print("=== 3. Rules (load + regex) ===")
        e, _, rules = tc.validate_rules(DATA_ROOT, cfg); errors += e
        print("=== 4. ECS field compliance ===")
        e, _ = tc.validate_ecs_fields(rules); errors += e
        print("=== 5. Program mapping ===")
        e, _ = tc.validate_program_mapping(cfg, rules); errors += e
        if skip_live:
            print("=== 6-9. Live checks ===")
            print("[INFO] skipped (live checks disabled)")
        else:
            print("=== 6. Network reachability (TCP) ===")
            errors += pf.check_network(cfg, timeout)
            print("=== 7. Kafka broker & topics ===")
            e, part_counts = pf.check_kafka(cfg, timeout); errors += e
            print("=== 8. Redis (for stateful rules) ===")
            errors += pf.check_redis(cfg, rules, timeout)
            print("=== 9. Workers vs partitions ===")
            pf.check_workers_vs_partitions(cfg, part_counts)

    return jsonify({
        "passed": errors == 0,
        "errors": errors,
        "lines": parse_report_lines(buf.getvalue()),
        "live_skipped": skip_live,
        "kafka_lib": KAFKA_LIB_OK,
        "redis_lib": REDIS_OK,
    })


# --------------------------------------------------------------------------- #
# Benchmark  ·  wraps the REAL benchmark.py (same one as the CLI), capturing
# its stdout — the UI and CLI can never diverge. Three modes:
#   capacity — per-rule EPS + parse latency + live utilization (CPU-heavy for
#              ~n_rules × seconds; capped)
#   live     — pipeline lag from the output NDJSON tails (read-only, fast)
#   history  — lag/EPS timeline computed by Elasticsearch; reuses the ELK
#              .env credentials the UI already signs in with, so no typing
# --------------------------------------------------------------------------- #
_BENCH_INTERVALS = ("15m", "30m", "1h", "3h", "1d")


@app.route("/api/benchmark/<mode>", methods=["POST"])
def api_benchmark(mode):
    try:
        import benchmark as bm
    except Exception as e:
        return jsonify({"error": f"benchmark tool unavailable in this build: {e}"}), 500
    from types import SimpleNamespace

    body = request.get_json(force=True, silent=True) or {}
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            if mode == "capacity":
                try:
                    seconds = float(body.get("seconds", 1.0))
                except (TypeError, ValueError):
                    seconds = 1.0
                seconds = min(5.0, max(0.2, seconds))
                args = SimpleNamespace(seconds=seconds,
                                       rule=(body.get("rule") or "").strip() or None,
                                       file=None)
                bm.run_synthetic(args, bm.load_config())
            elif mode == "live":
                try:
                    sample = int(body.get("sample", 500))
                except (TypeError, ValueError):
                    sample = 500
                args = SimpleNamespace(sample=min(5000, max(50, sample)))
                bm.run_live(args, bm.load_config())
            elif mode == "history":
                index = (body.get("index") or "").strip()
                if not index:
                    return jsonify({"error": "index pattern is required (e.g. fosstlsoc-logs-squid-*)"}), 400
                interval = body.get("interval") or "1h"
                if interval not in _BENCH_INTERVALS:
                    return jsonify({"error": f"interval must be one of {'/'.join(_BENCH_INTERVALS)}"}), 400
                password = (body.get("password") or "").strip()
                user = (body.get("user") or "").strip()
                if not password:
                    _, env = _find_elk_env()
                    if env:
                        password = env.get("ELASTIC_PASSWORD") or ""
                        user = user or env.get("ELASTIC_USERNAME") or ""
                if not password:
                    return jsonify({"error": "no Elasticsearch password available: none stored "
                                             "(ELK .env) and none entered in the form"}), 400
                try:
                    days = min(30, max(1, int(body.get("days", 3))))
                except (TypeError, ValueError):
                    days = 3
                args = SimpleNamespace(
                    es=(body.get("es") or "https://localhost:9200").rstrip("/"),
                    user=user or "elastic", password=password,
                    index=index, days=days, interval=interval)
                bm.run_history(args)
            else:
                return jsonify({"error": f"unknown benchmark mode: {mode}"}), 404
    except SystemExit as e:
        # benchmark.py reports usage/connectivity problems via sys.exit(msg)
        return jsonify({"error": str(e), "output": buf.getvalue()}), 400
    except Exception as e:
        return jsonify({"error": str(e), "output": buf.getvalue()}), 500
    return jsonify({"mode": mode, "output": buf.getvalue()})


# --------------------------------------------------------------------------- #
# Live monitoring  ·  reads what the running engine (main.py) writes into logs/
# (engine.pid heartbeat + per-worker stats), plus host CPU/RAM. All optional /
# dependency-free: psutil is used when present, otherwise /proc (Linux) or
# ctypes (Windows), and anything unavailable is simply reported as null.
# --------------------------------------------------------------------------- #
_CPU_PREV = {"idle": None, "total": None}


def read_json(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.loads(f.read())
    except Exception:
        return None


def _pid_alive(pid):
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    if os.name == "posix":
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        except Exception:
            return False
    try:  # Windows
        import ctypes
        PROCESS_QUERY_LIMITED = 0x1000
        h = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY_LIMITED, False, pid)
        if h:
            ctypes.windll.kernel32.CloseHandle(h)
            return True
        return False
    except Exception:
        return False


def _proc_rss(pid):
    try:
        import psutil
        return psutil.Process(int(pid)).memory_info().rss
    except Exception:
        pass
    p = f"/proc/{pid}/status"
    if os.path.exists(p):
        try:
            with open(p) as f:
                for line in f:
                    if line.startswith("VmRSS:"):
                        return int(line.split()[1]) * 1024
        except Exception:
            return None
    return None


def _host_cpu_percent():
    try:
        import psutil
        return round(psutil.cpu_percent(interval=None), 1)
    except Exception:
        pass
    try:
        with open("/proc/stat") as f:
            parts = f.readline().split()
        nums = [float(x) for x in parts[1:]]
        idle = nums[3] + (nums[4] if len(nums) > 4 else 0)
        total = sum(nums)
        prev_idle, prev_total = _CPU_PREV["idle"], _CPU_PREV["total"]
        _CPU_PREV["idle"], _CPU_PREV["total"] = idle, total
        if prev_idle is None:
            return None
        dt, di = total - prev_total, idle - prev_idle
        if dt <= 0:
            return None
        return round(100.0 * (1 - di / dt), 1)
    except Exception:
        return None


def _host_mem():
    try:
        import psutil
        m = psutil.virtual_memory()
        return {"total": m.total, "used": m.total - m.available, "percent": round(m.percent, 1)}
    except Exception:
        pass
    if os.path.exists("/proc/meminfo"):
        try:
            info = {}
            with open("/proc/meminfo") as f:
                for line in f:
                    k, _, v = line.partition(":")
                    info[k.strip()] = int(v.split()[0]) * 1024
            total = info.get("MemTotal", 0)
            avail = info.get("MemAvailable", info.get("MemFree", 0))
            used = total - avail
            return {"total": total, "used": used,
                    "percent": round(100.0 * used / total, 1) if total else 0}
        except Exception:
            pass
    if os.name == "nt":
        try:
            import ctypes

            class _MS(ctypes.Structure):
                _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                            ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                            ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                            ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                            ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]
            ms = _MS()
            ms.dwLength = ctypes.sizeof(_MS)
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(ms))
            return {"total": ms.ullTotalPhys, "used": ms.ullTotalPhys - ms.ullAvailPhys,
                    "percent": round(ms.dwMemoryLoad, 1)}
        except Exception:
            pass
    return None


def _load_avg():
    try:
        return [round(x, 2) for x in os.getloadavg()]
    except (OSError, AttributeError):
        return None


def _iso_age(ts_iso):
    if not ts_iso:
        return None
    try:
        from datetime import datetime
        t = datetime.fromisoformat(ts_iso)
        now = datetime.now(t.tzinfo) if t.tzinfo else datetime.now()
        return max(0.0, (now - t).total_seconds())
    except Exception:
        return None


def sys_metrics():
    return {
        "cpu_percent": _host_cpu_percent(),
        "cpu_count": os.cpu_count(),
        "mem": _host_mem(),
        "load": _load_avg(),
        "platform": platform.system(),
    }


def build_monitor():
    runtime = read_json(ENGINE_PID_PATH)
    pids, role, uptime, started_iso, kafka, workers_cfg = [], None, None, None, None, None
    if runtime:
        role = runtime.get("role")
        started_iso = runtime.get("started_iso")
        workers_cfg = runtime.get("workers")
        kafka = runtime.get("kafka")
        cand = list(runtime.get("worker_pids") or [])
        if runtime.get("pid"):
            cand.append(runtime["pid"])
        pids = sorted({int(p) for p in cand if _pid_alive(p)})
        st = runtime.get("start_time")
        if st:
            uptime = max(0, time.time() - st)
    pid_running = len(pids) > 0

    interval = (load_config().get("runtime") or {}).get("metrics_interval_sec", 2) or 2
    try:
        interval = float(interval)
    except (TypeError, ValueError):
        interval = 2.0
    stale_cut = max(20.0, interval * 8)

    # Pick stat files from the heartbeat's worker count, not a glob, so leftover
    # files from a previous run (e.g. ran 8 workers before, 1 now) are ignored.
    # Multi-worker -> per-worker files (summed live); single -> one stats.json.
    raw = []
    if runtime and role == "worker":
        s = read_json(os.path.join(LOG_DIR, "stats.json"))
        if s:
            raw = [s]
    elif runtime and role == "supervisor" and workers_cfg:
        for i in range(int(workers_cfg)):
            s = read_json(os.path.join(LOG_DIR, f"stats.w{i}.json"))
            if s:
                raw.append(s)
    else:
        # No live heartbeat (engine stopped / never ran): best-effort last data.
        wfiles = sorted(glob.glob(os.path.join(LOG_DIR, "stats.w*.json")))
        if wfiles:
            raw = [s for s in (read_json(w) for w in wfiles) if s]
        else:
            s = read_json(os.path.join(LOG_DIR, "stats.json"))
            if s:
                raw = [s]

    # Also drop any individually-stale file (a crashed worker that stopped
    # updating) so it doesn't inflate live totals.
    worker_stats = []
    for s in raw:
        a = _iso_age(s.get("timestamp"))
        if a is None or a <= stale_cut:
            worker_stats.append(s)

    eps = round(sum(w.get("eps", 0) or 0 for w in worker_stats), 2)
    total_processed = sum(w.get("total_processed", 0) or 0 for w in worker_stats)
    total_errors = sum((w.get("total_errors") or 0) for w in worker_stats)
    errors_window = sum((w.get("errors_window",
                                w.get("errors_last_min")) or 0)
                        for w in worker_stats)

    parser = {}
    for w in worker_stats:
        for rule, st in (w.get("parser_stats") or {}).items():
            agg = parser.setdefault(rule, {})
            for k, v in st.items():
                agg[k] = agg.get(k, 0) + (v or 0)

    ages = [a for a in (_iso_age(w.get("timestamp")) for w in worker_stats) if a is not None]
    stats_age = min(ages) if ages else None
    fresh = stats_age is not None and stats_age <= max(6, interval * 3)

    # Fresh stats also prove the engine is alive. This matters when the UI runs
    # in a container that can't see the engine's host PIDs (different PID
    # namespace) - pid liveness would be a false negative, so freshness backs it up.
    running = pid_running or fresh

    if not running:
        status = "stopped"
    elif not worker_stats or not fresh:
        status = "starting"
    else:
        status = "running"

    worker_rows = []
    for w in worker_stats:
        wpid = w.get("pid")
        worker_rows.append({
            "worker_id": w.get("worker_id"),
            "pid": wpid,
            "eps": w.get("eps", 0),
            "uptime_sec": w.get("uptime_sec"),
            "total_processed": w.get("total_processed", 0),
            "total_errors": w.get("total_errors", 0),
            "alive": _pid_alive(wpid) if wpid else None,
            "age": _iso_age(w.get("timestamp")),
        })

    engine_rss = 0
    for p in pids:
        rss = _proc_rss(p)
        if rss:
            engine_rss += rss

    dlq_bytes = 0
    # old flat files (logs/dlq*.json) AND the per-source folder the engine
    # writes today (logs/dlq/<program>.wN.json + rotated .1) — audit A1#7:
    # the tile silently showed 0 because it only looked at the flat names.
    for d in glob.glob(os.path.join(LOG_DIR, "dlq*.json")):
        try:
            dlq_bytes += os.path.getsize(d)
        except Exception:
            pass
    for root, _dirs, files in os.walk(os.path.join(LOG_DIR, "dlq")):
        for name in files:
            try:
                dlq_bytes += os.path.getsize(os.path.join(root, name))
            except Exception:
                pass

    return {
        "status": status,
        "running": running,
        "role": role,
        "started_iso": started_iso,
        "pids": pids,
        "workers": workers_cfg or len(worker_rows),
        "workers_alive": len(pids) or (len(worker_stats) if fresh else 0),
        "pid_visible": pid_running,
        "uptime_sec": int(uptime) if uptime is not None else None,
        "kafka": kafka,
        "eps": eps,
        "total_processed": total_processed,
        "total_errors": total_errors,
        "errors_window": errors_window,
        "stats_age_sec": round(stats_age, 1) if stats_age is not None else None,
        "stats_fresh": fresh,
        "metrics_interval": interval,
        "parser_stats": parser,
        "workers_detail": worker_rows,
        "engine_rss": engine_rss,
        "dlq_bytes": dlq_bytes,
        "system": sys_metrics(),
        "control_enabled": os.environ.get("SOC_UI_ALLOW_CONTROL") == "1",
        "now": time.time(),
    }


@app.route("/api/monitor")
def api_monitor():
    return jsonify(build_monitor())


@app.route("/api/monitor/dlq")
def api_monitor_dlq():
    try:
        n = int(request.args.get("n", 20))
    except (TypeError, ValueError):
        n = 20
    entries = []
    # New layout: per-source files under logs/dlq/ ; old layout: logs/dlq*.json
    paths = sorted(glob.glob(os.path.join(LOG_DIR, "dlq", "*.json"))) + \
        sorted(glob.glob(os.path.join(LOG_DIR, "dlq*.json")))
    for d in paths:
        try:
            with open(d, "r", encoding="utf-8", errors="ignore") as f:
                for ln in f.readlines()[-n:]:
                    ln = ln.strip()
                    if not ln:
                        continue
                    try:
                        entries.append(json.loads(ln))
                    except Exception:
                        entries.append({"raw": ln})
        except Exception:
            continue
    return jsonify({"entries": entries[-n:], "count": len(entries)})


@app.route("/api/engine/<action>", methods=["POST"])
def api_engine_control(action):
    """Start/stop/restart the engine via systemd. Off by default; enable with
    SOC_UI_ALLOW_CONTROL=1 and make sure the UI user may run systemctl."""
    if action not in ("start", "stop", "restart", "status"):
        return jsonify({"error": "unknown action"}), 400
    if action != "status" and os.environ.get("SOC_UI_ALLOW_CONTROL") != "1":
        return jsonify({"error": "Engine control is disabled. Start the UI with "
                        "SOC_UI_ALLOW_CONTROL=1 (and allow the UI user to run "
                        "systemctl) to enable start/stop/restart."}), 403
    if os.name == "nt":
        return jsonify({"error": "Service control is Linux/systemd only."}), 400
    if not shutil.which("systemctl"):
        return jsonify({"error": "systemctl not found; manage the engine manually."}), 400

    service = os.environ.get("SOC_ENGINE_SERVICE", "foss-soc")
    cmd = (["systemctl", "is-active", service] if action == "status"
           else ["systemctl", action, service])
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=25)
        out = (proc.stdout + proc.stderr).strip()
        ok = proc.returncode == 0 or (action == "status" and proc.returncode in (0, 3))
        return jsonify({"action": action, "service": service, "ok": ok,
                        "returncode": proc.returncode, "output": out or "(no output)"})
    except subprocess.TimeoutExpired:
        return jsonify({"error": f"systemctl {action} timed out"}), 504
    except Exception as e:  # noqa: BLE001
        return jsonify({"error": str(e)}), 500


# --------------------------------------------------------------------------- #
# Entrypoint
# --------------------------------------------------------------------------- #
def open_browser(url):
    import threading
    import webbrowser

    def _open():
        import time
        time.sleep(1.2)
        try:
            webbrowser.open(url)
        except Exception:
            pass
    threading.Thread(target=_open, daemon=True).start()


def _lan_ip():
    """Best-effort primary LAN IP of this machine (for the startup banner)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))  # no packets sent; just picks the route
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()


def main():
    host = os.environ.get("SOC_UI_HOST", "127.0.0.1")
    port = int(os.environ.get("SOC_UI_PORT", "8600"))
    all_ifaces = host in ("0.0.0.0", "::")

    # On a server (listening on all interfaces) there is usually no browser to
    # open and we want the network URL, so default to no-browser unless the
    # operator explicitly set SOC_UI_NO_BROWSER=0.
    env_nb = os.environ.get("SOC_UI_NO_BROWSER")
    no_browser = (env_nb == "1") or (all_ifaces and env_nb != "0")

    local_url = f"http://127.0.0.1:{port}/"
    if all_ifaces:
        net_url = f"http://{_lan_ip()}:{port}/"
        access = (f"  On this machine:       {local_url}\n"
                  f"  From the network:      {net_url}\n"
                  "  (listening on ALL interfaces - make sure the firewall allows "
                  f"port {port})\n")
        open_url = local_url
    else:
        access = (f"  Open in your browser:  http://{host}:{port}/\n"
                  "  (local only - set SOC_UI_HOST=0.0.0.0 to reach it over the "
                  "network)\n")
        open_url = f"http://{host}:{port}/"

    if AUTH_DISABLED:
        login_line = "  Login:                 (authentication disabled - SOC_UI_NO_AUTH=1)\n"
    elif AUTH_FROM_ELK:
        login_line = (f"  Login:                 {AUTH_USER} / <your ELK/Elastic password>  "
                      "(from TLSOCDocker .env)\n")
    elif AUTH_SOURCE == "env-vars":
        login_line = (f"  Login:                 {AUTH_USER} / <your SOC_UI_PASSWORD>\n")
    else:
        login_line = (f"  Login:                 {AUTH_USER} / <generated - see the "
                      "FIRST RUN banner above; reset: delete .soc-ui-auth.json>\n")

    print(
        "\n  FOSS SOC Engine - Web UI\n"
        + access
        + login_line
        + f"  Data folder:           {DATA_ROOT}\n"
        + f"  Rules:                 {rules_dir()}\n"
        + "  Press Ctrl+C to stop.\n"
    )

    if not no_browser:
        open_browser(open_url)

    # debug=False / use_reloader=False so it behaves the same frozen or not.
    app.run(host=host, port=port, debug=False, use_reloader=False, threaded=True)


if __name__ == "__main__":
    main()
