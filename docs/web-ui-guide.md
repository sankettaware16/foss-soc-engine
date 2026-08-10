# TLSOC Engine — Web UI Guide

A point‑and‑click console for the log‑parsing engine. Everything you used to do
by running separate Python files — testing a log file, adding and testing a
parser, validating the config — now happens in your **browser**. No terminal
required.

This guide is written for **non‑technical operators**. If you can open a web
page, you can use this.

---

## 1. Three ways to start it

You only need **one** of these. Pick whichever matches what you downloaded.

### A) The Windows app (easiest — no Python, no install)

1. Unzip the `FOSS-SOC-UI` folder anywhere (Desktop is fine).
2. Double‑click **`FOSS-SOC-UI.exe`**.
3. A small black window opens and your browser pops up at
   **http://127.0.0.1:8600**. That's the console.
4. To stop it: close the browser tab, then press **Ctrl+C** in the black window
   (or just close that window).

> Windows SmartScreen may show "Windows protected your PC" the first time
> (because the app isn't code‑signed). Click **More info → Run anyway**. This is
> expected for a freshly built, unsigned testing app.

The folder next to the .exe contains everything the app reads and you can edit:

```
FOSS-SOC-UI/
├─ FOSS-SOC-UI.exe      ← double-click this
├─ config.yaml          ← your engine settings (editable in the UI too)
├─ rules/               ← your parser rules (.yaml) — editable in the UI too
├─ examples/            ← sample logs to try
└─ database/            ← (optional) drop GeoLite2-City.mmdb + GeoLite2-ASN.mmdb here for geo/ASN data
```

### B) The launcher script (you already have Python 3)

- **Windows:** double‑click `webui/Start-SOC-UI.bat`
- **Linux / macOS:** run `./webui/start-soc-ui.sh`

The first run quietly creates a private environment and installs two tiny
packages (Flask + PyYAML). Every run after that just opens the console.

### C) Straight from Python (for developers)

```bash
pip install -r webui/requirements-ui.txt
python webui/app.py
```

Then open **http://127.0.0.1:8600** if it doesn't open on its own.

---

### Signing in

The console is password-protected, and the startup log **always tells you which
login it is using** — when in doubt, look there first:

```bash
journalctl -u foss-soc-ui -n 40 --no-pager | grep '\[auth\]'   # server (systemd)
# or just read the black console window on Windows
```

**Case A — you run the TLSOC Docker Deploy stack (recommended for servers).**
Use the **same `elastic` username and password as Kibana**. Tell the console
where your stack's `.env` lives by adding this to `config.yaml` — careful, the
second line is indented under `auth:`:

```yaml
auth:
  env_file: "/opt/TLSOCDockerDeploy/.env"
```

Then `sudo systemctl restart foss-soc-ui` and check the log line says
`[auth] credentials from elk-env:...`. Sign in as `elastic`. While the `.env` is
in use the generated local login is disabled (no weaker back-door), and if the
path is wrong the log prints an `[auth] WARNING` explaining why it fell back.
(You can use the `SOC_ENV_FILE` environment variable instead of the config block —
same effect.)

**Case B — anywhere else (e.g. the Windows app).** On **first start** the console
window prints a **generated password** for user `admin` (there is no built-in
default login). It is stored salted-and-hashed in `.soc-ui-auth.json` next to the
app. Lost it?

- On a server: `journalctl -u foss-soc-ui --no-pager | grep 'password:'`
- Anywhere: **delete `.soc-ui-auth.json` and restart** — a new password is
  generated and printed.
- Prefer choosing your own? Set `SOC_UI_USER` and `SOC_UI_PASSWORD` before
  launching.

Where it looks for the ELK `.env` (first match wins): the `SOC_ENV_FILE`
environment variable → `auth.env_file` in `config.yaml` → a
`TLSOCDockerDeploy/.env` next to the app → a `.env` in the app folder.

> **Server tip:** the systemd unit must contain `Environment=PYTHONUNBUFFERED=1`
> (the shipped `webui/foss-soc-ui.service` has it) — without it the `[auth]` line
> and the first-run password can be held back from `journalctl` by buffering.

Sign out any time with the button in the top-right. For local development only,
you can disable the login entirely with `SOC_UI_NO_AUTH=1`.

> The login travels over plain HTTP, so use the console on a trusted network
> (see §12). For wider exposure, put it behind an HTTPS reverse proxy.

---

## 2. The console at a glance

The left sidebar sections:

| Section        | What it's for |
|----------------|---------------|
| **Dashboard**  | A health overview: how many rules loaded, which strategies, and which optional features (Redis/GeoIP/etc.) are available. |
| **Monitor**    | A **live** view of the running engine — events/sec, totals, errors, uptime, which rules are busy, per-worker stats and CPU/RAM. Updates itself; no refresh needed. |
| **Test Log**   | The main tool. Paste log lines or upload a file, choose a parser, and see the parsed result. |
| **Rules**      | View, edit, create and delete parser rules — with live ECS field checking. |
| **Config**     | Edit `config.yaml` and validate it before going live. |
| **ECS Helper** | Look up the correct field name for anything ("country", "http status"…) or autocorrect a wrong one. |
| **Preflight**  | When you're ready to connect to the real Kafka/Redis pipeline, run the readiness checks. |
| **Benchmark**  | Measure what this machine can parse (EPS, latency, utilization %), check the pipeline is real-time, or replay how it handled a past traffic spike. |

The colored dot at the top‑right is the engine status (green = healthy).

---

## 3. Monitor — watch the engine run in real time

This is the live operations view, most useful when the engine runs as a service.
It refreshes itself every couple of seconds — **you never reload the page**.
Click **Monitor** in the sidebar.

A big status light shows the engine state:

- 🟢 **Running** — the engine is alive and writing fresh stats.
- 🟡 **Starting / stale** — the process is up but hasn't reported recently (just
  launched, or it isn't receiving any logs).
- 🔴 **Stopped** — the engine is not running.

Next to it: **uptime**, how many **workers** are alive, the Kafka **topic** and
consumer **group**, and the process **PID(s)**. Below that, live panels:

- **Events / sec (EPS)** — current throughput, with a moving graph of the last
  few minutes so you can see spikes and dips.
- **Total processed**, **Total errors**, **Engine RAM**.
- **System resources** — host CPU %, memory used/total, load average, cores.
- **Rules in use** — every rule with how many events it parsed plus its
  no-match / buffered / **expired** / error counts (instantly shows which parser
  is busy and which is failing). *Expired* = multi-line transactions that timed
  out before completing — they are still emitted (tagged
  `event.incomplete: true`), this column just makes them visible.
- **Workers** — one row per worker: PID, its own EPS, events processed, uptime
  and a green/red alive light.
- **Recent errors / DLQ** — click *Load latest* to see the most recent
  un-parseable lines and why they failed. On disk each source gets its own
  dead-letter file (`logs/dlq/nginx.json`, `logs/dlq/postfix.json`, …), capped
  in size, so one broken source can't fill the disk — and if a source suddenly
  dead-letters heavily, `engine.log` gets a **"DLQ STORM"** warning naming it.

> **Where the numbers come from.** The running engine writes tiny stats files
> (every 2 s by default) plus a heartbeat into its `logs/` folder, and the
> Monitor reads those. So the UI and the engine must share the same project
> folder (they do when both run from e.g. `/opt/foss-soc-engine`). If they live
> in different places, point the UI at the engine's logs with the `SOC_LOG_DIR`
> environment variable. Tune how often stats are written with
> `runtime.metrics_interval_sec` in `config.yaml`.

### Start / stop / restart from the browser (optional)

By default the Monitor only *watches*. To also get **Start / Stop / Restart**
buttons (Linux + systemd only), launch the UI with `SOC_UI_ALLOW_CONTROL=1` and
make sure its user may run `systemctl` for the engine service (service name from
`SOC_ENGINE_SERVICE`, default `foss-soc`). It is **off by default** so a
networked console can't be used to stop your engine.

---

## 4. Test a log file or a single line

This answers the question **"will my parser understand these logs?"**

1. Click **Test Log**.
2. Choose a **Parser**:
   - **AUTO** — let the engine try every rule and pick the one that matches
     (best when you're not sure which parser fits).
   - A specific rule (e.g. `apache_access`) — to test exactly that parser.
3. Give it some logs, either way:
   - **Paste text** — paste one or more raw log lines. (Click **Load sample
     lines** to try it instantly.)
   - **Upload file** — choose or drag‑drop a `.log` file. Big files are capped
     at the **Max lines** number so the browser stays fast.
4. Click **Run test ⚡**.

You'll get:

- **Parsed events** — the count of log lines turned into structured ECS JSON.
- **Match rate** — what % of lines the parser understood.
- **No match / Errors** — lines it couldn't parse (with a few examples so you
  can see *why*).
- The actual **ECS JSON** for each parsed line. Click **Copy JSON** to grab it.

> **Reading the result:** a healthy parser shows a high match rate and real
> field values (e.g. `source.ip`, `http.response.status_code`). If match rate is
> low, the regex in the rule probably doesn't fit these log lines — open the
> **Rules** tab and adjust it, then test again.

> **Check the time fields too:** `@timestamp` should show the time **written in
> the log line** (converted to UTC), not the moment you clicked Run — that's what
> makes delayed logs land on the right day in Elasticsearch. `event.ingested` is
> when it was parsed, and `event.timestamp_source` tells you where the time came
> from: `log` is good; `log_assumed_utc` means the log had no timezone (add
> `tz: "+05:30"`-style to the rule's `timestamp:` block if you know it);
> `ingest_fallback` means the time couldn't be read — add or fix the rule's
> `timestamp:` block (see [writing-rules.md](writing-rules.md) §5).

---

## 5. Add or edit a parser (a "rule")

A parser is just a small YAML file that says *how* to pull fields out of a log
line. You never have to leave the browser to write one.

**To edit an existing rule:**

1. Click **Rules**.
2. Click a rule on the left. Its YAML opens in the editor.
3. Make changes, then click **Check ECS fields** to confirm your field names are
   valid, and **Save rule** when happy.

**To add a new parser:**

1. Click **Rules → + New**.
2. Pick a **Template** that matches your log type:
   - `stateless` — one regex, single‑line logs (Apache/Nginx access logs).
   - `multi_match` — several regexes, first match wins (Linux auth: ssh/sudo).
   - `stateful` — multi‑line events tied together by an id (mail logs; needs
     Redis at run time).
   - `json_map` — JSON logs (map by dotted path).
   - `xml_xpath` — XML logs (scanner output).
3. Give it a **File name** (e.g. `myapp.yaml`).
4. Edit the template's regex and field mappings.
5. **Check ECS fields**, then **Save rule**.
6. Hop to **Test Log**, choose your new parser, and confirm it works.

> **Tip:** field names should follow ECS (Elastic Common Schema). The editor's
> **Check ECS fields** button flags any wrong name and shows the correct one. A
> red ✗ means "fix this"; an amber ~ means "custom field, allowed".

---

## 6. Edit and validate the config

1. Click **Config**.
2. Edit `config.yaml` (Kafka servers, topic, workers, paths, program mappings…).
3. Click **Save**.
4. Click **Validate** to run the same safety checks the command‑line validator
   does — structure, paths, every rule's regex, ECS compliance, and that each
   `program_mapping` points at a real rule.

A green **Passed** means the config is structurally sound. Warnings are usually
fine to start with (e.g. an output folder that doesn't exist yet).

---

## 7. ECS Helper (field name lookup)

Not sure what to call a field? Click **ECS Helper**.

- **Check / autocorrect** — type a name like `srcip` and it tells you to use
  `source.ip`. Type a real one like `user.name` and it confirms it's valid.
- **Find by concept** — type plain words like `country` or `http status` and it
  lists the matching ECS fields.

Use this while writing a rule so your fields are correct the first time.

---

## 8. Preflight (only when connecting the real pipeline)

The Web UI is mainly for **local testing**, which needs nothing but the app
itself. When you're ready to point it at a live **Kafka** cluster (and **Redis**
for stateful rules), use **Preflight**:

1. Click **Preflight**.
2. Tick **include live checks** to actually reach out to Kafka/Redis/TCP, or
   untick it for static‑only checks.
3. Click **Run preflight ✈**.

It reports, step by step, whether the config is sound, whether the Kafka host is
reachable, whether the topics exist, and whether Redis is up. Green all the way
down means it's safe to start the engine itself (`python main.py`).

---

## 9. Benchmark (how fast is my setup, and is it keeping up?)

Click **Benchmark**. Three tools, one page — results appear in the box below
them:

1. **Capacity** — "how much can this machine parse?" Runs every rule against
   its sample corpus and reports events/second per rule, parse latency
   (p50 = the typical event, p95/p99 = the slowest 1‑in‑20 / 1‑in‑100), and —
   when the engine is running — your **current utilization** as a percentage
   with a bar ("you use 71 EPS of ~19,000 = 0.4%"). Expect the machine to be
   busy for ~15–60 seconds while it measures.
2. **Live lag** — "is the pipeline real‑time *right now*?" Reads the newest
   events from each module's output file and shows how long ago each event
   really happened (lag = `event.ingested − @timestamp`). Steady sub‑second
   numbers are healthy; a **negative** or hours‑sized lag means a *source
   host's* clock or timezone label is wrong — not the engine.
3. **History** — "how did it behave last week / during that big onboarding?"
   Elasticsearch rebuilds the lag + EPS timeline from the events already
   stored, bucket by bucket, with flags (`lagging`, `BEHIND`, `clock/tz?`).
   Enter the index pattern (e.g. `fosstlsoc-logs-squid-*`); the password can
   stay empty — the console reuses the same ELK sign‑in it already has.
   Large patterns can take a minute.

Same engine code as the CLI (`python3 benchmark.py`, `--live`, `--history`),
so the numbers in the browser and the terminal always agree.

---

## 10. What works without extra software

The console is built to **never crash on a missing dependency**:

| Feature | Needs | If missing |
|---|---|---|
| Test Log, Rules, Config, ECS Helper | nothing extra | always works |
| Geo enrichment (country/city on IPs) | `geoip2` + `GeoLite2-City.mmdb` | quietly skipped (no geo fields) |
| ASN enrichment (which ISP/cloud owns the IP) | `geoip2` + `GeoLite2-ASN.mmdb` | quietly skipped (no `source.as` fields) |
| Stateful parsers (mail correlation) | `redis` + a Redis server | lines are recognized but not correlated; a notice is shown |
| Preflight live checks | `kafka-python-ng`, `redis` | the check reports them as unavailable instead of erroring |

So you can test parsers and rules on a bare machine with **nothing installed**,
and add Redis/GeoIP/Kafka later only when you actually go live.

---

## 11. Changing the port or stopping it

- **Port:** set an environment variable before launching:
  `SOC_UI_PORT=9000`. (Default is `8600`.)
- **Don't auto‑open the browser:** set `SOC_UI_NO_BROWSER=1`.
- **Listen on the network (not just this PC):** set `SOC_UI_HOST=0.0.0.0`
  (see the next section).
- **Stop it:** press **Ctrl+C** in the console window, or just close it.

---

## 12. Run it on an Ubuntu server & open it over the network

By default the console only answers on the machine it runs on (`127.0.0.1`).
To reach it from other computers, bind it to **all interfaces** with
`SOC_UI_HOST=0.0.0.0`, then browse to the **server's real IP**.

> ⚠️ **Security first.** The UI has **no login** and can edit rules/config and
> read files on the server. Only expose it on a **trusted/internal network or a
> VPN**, and lock the port down with the firewall to the machines that need it.
> Do **not** put it directly on the public internet.

### Step 1 — get the code onto the server

The Windows `.exe` does **not** run on Linux. On Ubuntu you run it from Python
(no compiler needed — only Flask + PyYAML, which install as wheels):

```bash
# copy the project to the server, e.g. /opt/foss-soc-engine, then:
cd /opt/foss-soc-engine
./webui/start-soc-ui.sh        # first run makes a venv + installs Flask/PyYAML
```

(Need `sudo apt install -y python3 python3-venv` first if Python isn't present.)

### Step 2 — find the server's IP

```bash
hostname -I        # e.g. 192.168.10.25
```

### Step 3 — start it bound to the network

```bash
cd /opt/foss-soc-engine
SOC_UI_HOST=0.0.0.0 SOC_UI_PORT=8600 SOC_UI_NO_BROWSER=1 ./webui/start-soc-ui.sh
```

On start it prints both URLs, e.g.:

```
  On this machine:    http://127.0.0.1:8600/
  From the network:   http://192.168.10.25:8600/
```

### Step 4 — open the firewall for that port

```bash
sudo ufw allow 8600/tcp                 # allow from anywhere on the LAN, OR
sudo ufw allow from 192.168.10.0/24 to any port 8600 proto tcp   # only your subnet
```

### Step 5 — browse from any PC on the network

Open **`http://192.168.10.25:8600`** (use your server's IP). Done.

### Keep it running after you log out (recommended: systemd)

A ready‑made service file is included at `webui/foss-soc-ui.service`:

```bash
sudo cp webui/foss-soc-ui.service /etc/systemd/system/
sudoedit /etc/systemd/system/foss-soc-ui.service   # set User, WorkingDirectory, IP/port
sudo systemctl daemon-reload
sudo systemctl enable --now foss-soc-ui            # start now + on every boot
systemctl status foss-soc-ui                       # is it up?
journalctl -u foss-soc-ui -f                       # watch its logs
```

Now the console is always available at `http://<server-ip>:8600`, survives
reboots, and restarts itself if it crashes.

> **Quick‑and‑dirty alternative** (no systemd): `nohup`, `screen`, or `tmux`:
> ```bash
> SOC_UI_HOST=0.0.0.0 SOC_UI_NO_BROWSER=1 nohup ./webui/start-soc-ui.sh &>/tmp/soc-ui.log &
> ```

> **Want a Linux executable too?** Run `python3 webui/build_exe.py` **on the
> Ubuntu box** — PyInstaller produces a native `FOSS-SOC-UI` binary in
> `release/FOSS-SOC-UI/` (binaries are OS‑specific, so build on the OS you'll run
> on).

---

## 13. Quick troubleshooting

- **Browser didn't open** → manually visit http://127.0.0.1:8600
- **"Port already in use"** → something is already on 8600; relaunch with
  `SOC_UI_PORT=8601`.
- **"No rules loaded"** → make sure the `rules/` folder sits next to the .exe
  (or in the project root when running from Python).
- **SmartScreen warning on Windows** → More info → Run anyway (unsigned testing
  build).
- **Match rate is 0%** → wrong parser chosen, or the rule's regex doesn't fit
  the log format. Try **AUTO**, or open the rule and adjust the regex.
- **Monitor says "Stopped" but the engine is running** → the UI is reading a
  different `logs/` folder than the engine writes to. Run both from the same
  project folder, or set `SOC_LOG_DIR` for the UI to the engine's `logs/` path.
- **Monitor says "Starting / stale"** → the engine is up but no fresh stats:
  it just started, or it isn't receiving any logs from Kafka yet.
- **Control buttons missing / "control disabled"** → start the UI with
  `SOC_UI_ALLOW_CONTROL=1` (Linux + systemd) to enable start/stop/restart.

---

*This UI is a front‑end over the same engine described in `README.md`. It does
not change how the engine parses or ships logs — it just makes testing and rule
authoring click‑and‑go.*
