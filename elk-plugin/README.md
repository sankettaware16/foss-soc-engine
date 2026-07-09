# TLSOC Parser — Kibana plugin

Use the **entire FOSS SOC parsing console directly inside Kibana** — test logs,
add/edit parser rules, edit & validate config, ECS lookup, and the live engine
Monitor — as a native item in the Kibana left navigation. No separate web app,
no separate login (Kibana already authenticates you).

This is an **optional** front-end. The standalone Web UI (`webui/`) still works
exactly as before; this plugin is a second way to reach the same capabilities,
for teams that live in Kibana.

---

## Architecture (why it's built this way)

```
   Browser (Kibana)                Kibana server (Node)          Engine host
 ┌───────────────────┐          ┌──────────────────────┐     ┌────────────────────┐
 │ TLSOC Parser app  │  /api/   │  plugin server        │     │ tlsoc-parser-ui     │
 │ (React + EUI)     │ ───────▶ │  proxy route          │────▶│ (Flask, headless)   │
 │  Test / Rules /   │  tlsoc_  │  /api/tlsoc_parser/*  │ http │  the REAL engine    │
 │  Config / ECS /   │  parser  │        │              │     │  UniversalEngine,   │
 │  Monitor tabs     │          │        ▼              │     │  test_config, ...   │
 └───────────────────┘          └──────────────────────┘     └─────────┬──────────┘
        ▲  inherits Kibana login / RBAC                                 │ bind-mount
        │                                                     rules/ · config.yaml · logs/
        └───────────────────── one API contract ─────────────▶  (the live systemd engine)
```

Three deliberate choices:

1. **The Python engine stays the single source of truth.** The plugin never
   re-implements parsing. Its server side is a *thin generic proxy* that forwards
   `/api/tlsoc_parser/<path>` to the Flask backend's `/api/<path>` — the **same
   API contract** the standalone Web UI uses. So the browser code, the Web UI,
   and the CLI can never diverge.

2. **Easy to update.** Because the proxy is a catch-all, a new engine feature /
   endpoint is instantly reachable from the plugin with **no plugin server
   change** — only a React page needs adding for the new UI. Backend logic lives
   in one place (Python).

3. **The backend runs as a container** (`tlsoc-parser-ui`) on your existing
   Docker network, bind-mounted to the **real engine's** `rules/`, `config.yaml`
   and `logs/`. So editing a rule in Kibana edits the production rule (the engine
   hot-reloads it), and the Monitor shows the live engine's real EPS/metrics.

**No plugin login.** Kibana authenticates the user and the proxy runs under that
session; the backend is internal-only (`SOC_UI_NO_AUTH=1`, no host port). The
standalone Web UI keeps its own login for when it's used outside Kibana.

---

## Folder layout

```
elk-plugin/
├─ README.md                     ← this file (architecture + overview)
├─ INSTALL.md                    ← exact build + deploy steps for your stack
├─ backend/
│  └─ Dockerfile                 ← runs webui/app.py headless as tlsoc-parser-ui
├─ deploy/
│  └─ docker-compose.snippet.yml ← service + kibana mounts to add to your compose
└─ kibana-plugin/                ← the Kibana plugin source (TypeScript/React/EUI)
   ├─ kibana.json                ← plugin manifest (kibanaVersion 8.19.12)
   ├─ package.json / tsconfig.json
   ├─ public/                    ← the React/EUI app (nav item + tabs)
   └─ server/                    ← the proxy route + config
```

---

## Feature parity with the Web UI

| Web UI tab   | In the Kibana plugin | Backend endpoint(s) proxied |
|--------------|----------------------|------------------------------|
| Test Log     | ✅ paste/upload, AUTO or specific parser, ECS JSON output | `/api/test` |
| Rules        | ✅ list / view / **create** / edit / delete + ECS check | `/api/rules*`, `/api/ecs/check` |
| Config       | ✅ edit + validate | `/api/config*` |
| ECS Helper   | ✅ classify + find | `/api/ecs/*` |
| Monitor      | ✅ live EPS, rules, workers, CPU/RAM, DLQ | `/api/monitor*` |
| Preflight    | ✅ readiness checks | `/api/preflight` |

---

## Quick start

See **[INSTALL.md](INSTALL.md)** for the full walkthrough. In short:

1. **Build the backend image** (from the engine repo root):
   `docker build -f elk-plugin/backend/Dockerfile -t tlsoc-parser-ui:1.0.0 .`
   > Rebuild this image whenever the engine's Python code changes (e.g. `core/`,
   > `webui/`) — the image bakes the code in; only `rules/` + `config.yaml` +
   > `logs/` come from the bind-mount at runtime.
2. **Build the Kibana plugin** against a Kibana 8.19.12 dev tree (produces a
   folder/zip) and drop it in `./kibana/installed_plugins/tlsocParser`.
3. **Wire it into your compose** (`deploy/docker-compose.snippet.yml`): add the
   `tlsoc-parser-ui` service and the `kibana` volume + `TLSOCPARSER_BACKENDURL`.
4. `docker compose up -d --build tlsoc-parser-ui kibana`
5. Open Kibana → left nav → **TLSOC Parser**.
