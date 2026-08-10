/* ============================================================== *
 *  FOSS SOC Engine - Web UI  ·  frontend logic (vanilla JS)
 * ============================================================== */

const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];

const VIEW_META = {
  dashboard: ["Dashboard", "Live overview of your parsing engine"],
  monitor: ["Monitor", "Real-time engine health, throughput & resource metrics"],
  test: ["Test Log", "Run raw logs through any parser and see the ECS output"],
  rules: ["Rules", "View, edit, create and validate parser rules"],
  config: ["Config", "Edit config.yaml and validate it before going live"],
  ecs: ["ECS Helper", "Look up and autocorrect Elastic Common Schema fields"],
  preflight: ["Preflight", "Readiness checks for the live Kafka / Redis pipeline"],
  benchmark: ["Benchmark", "Capacity, live pipeline lag and historical performance"],
  help: ["How to use", "A quick tour of the console"],
};

/* ---------- tiny helpers ---------- */
async function api(path, opts) {
  const res = await fetch(path, opts);
  if (res.status === 401) {
    window.location.href = "/login";
    throw new Error("Session expired — redirecting to sign in");
  }
  let data;
  try { data = await res.json(); } catch { data = {}; }
  if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
  return data;
}
function toast(msg, kind = "") {
  const t = $("#toast");
  t.textContent = msg;
  t.className = "toast show " + kind;
  clearTimeout(toast._t);
  toast._t = setTimeout(() => (t.className = "toast"), 2600);
}
function esc(s) {
  return String(s).replace(/[&<>]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" }[c]));
}
function jsonHighlight(obj) {
  const json = esc(JSON.stringify(obj, null, 2));
  return json
    .replace(/("(\\u[a-zA-Z0-9]{4}|\\[^u]|[^\\"])*")(\s*:)?/g, (m, p1, p2, p3) =>
      p3 ? `<span class="jkey">${p1}</span>${p3}` : `<span class="jstr">${p1}</span>`)
    .replace(/\b(true|false|null)\b/g, '<span class="jbool">$1</span>')
    .replace(/(:\s*)(-?\d+\.?\d*)/g, '$1<span class="jnum">$2</span>');
}

/* ---------- navigation ---------- */
function showView(name) {
  $$(".view").forEach((v) => v.classList.remove("active"));
  const el = $("#view-" + name);
  if (el) el.classList.add("active");
  $$(".nav-item").forEach((n) => n.classList.toggle("active", n.dataset.view === name));
  const [title, sub] = VIEW_META[name] || ["", ""];
  $("#view-title").textContent = title;
  $("#view-sub").textContent = sub;
  if (name === "dashboard") loadHealth();
  if (name === "rules") loadRules();
  if (name === "config") loadConfig();
  if (name === "test") populateParserSelect();
  if (name === "monitor") startMonitor(); else stopMonitor();
}
document.addEventListener("click", (e) => {
  const nav = e.target.closest("[data-view]");
  if (nav) { e.preventDefault(); showView(nav.dataset.view); }
});

/* ============================================================== *
 *  Dashboard / health
 * ============================================================== */
let HEALTH = null;
async function loadHealth() {
  try {
    const h = await api("/api/health");
    HEALTH = h;
    $("#status-dot").className = "status-dot ok";
    $("#status-text").textContent = `${h.rules_count} rules · py ${h.python}`;
    renderHealth(h);
  } catch (e) {
    $("#status-dot").className = "status-dot bad";
    $("#status-text").textContent = "engine error";
    toast(e.message, "bad");
  }
}
function renderHealth(h) {
  // stat cards
  const strat = Object.entries(h.strategies || {}).map(([k, v]) => `${k}:${v}`).join("  ");
  const stats = [
    { v: h.rules_count, k: "Parser rules", cls: "acc" },
    { v: Object.keys(h.strategies || {}).length, k: "Strategies in use", cls: "" },
    { v: Object.keys(h.program_mapping || {}).length, k: "Program mappings", cls: "" },
    { v: h.config_found ? "OK" : "—", k: "config.yaml", cls: h.config_found ? "good" : "warn" },
  ];
  $("#stat-cards").innerHTML = stats.map((s) =>
    `<div class="stat ${s.cls}"><div class="v">${s.v}</div><div class="k">${s.k}</div></div>`).join("");

  // rule chips
  $("#dash-rules").innerHTML = (h.rules || []).map((r) =>
    `<span class="chip" title="${r.file}">${esc(r.name)}<span class="strat">${r.strategy}</span></span>`
  ).join("") || '<span class="muted small">No rules loaded.</span>';

  // capability badges
  const caps = [
    ["Redis (stateful rules)", h.capabilities.redis],
    ["GeoIP enrichment", h.capabilities.geoip],
    ["orjson (speed)", h.capabilities.orjson],
    ["Kafka client", h.capabilities.kafka],
  ];
  $("#dash-caps").innerHTML = caps.map(([n, on]) =>
    `<div class="cap"><span>${n}</span><span class="badge ${on ? "on" : "off"}">${on ? "available" : "not installed"}</span></div>`
  ).join("");

  // sidebar pills
  $("#health-pills").innerHTML = caps.map(([n, on]) =>
    `<span class="pill ${on ? "on" : "off"}" title="${n}"><span class="d"></span>${n.split(" ")[0]}</span>`
  ).join("");

  // load errors
  if (h.load_errors && h.load_errors.length) {
    $("#dash-errors-card").style.display = "";
    $("#dash-errors").innerHTML = h.load_errors.map((e) =>
      `<div class="logline ERROR"><span class="lvl">${esc(e.file)}</span>${esc(e.error)}</div>`).join("");
  } else {
    $("#dash-errors-card").style.display = "none";
  }
}

/* ============================================================== *
 *  Test Log
 * ============================================================== */
function populateParserSelect() {
  if (!HEALTH) { loadHealth().then(() => HEALTH && fillParsers()); return; }
  fillParsers();
}
function fillParsers() {
  const sel = $("#test-parser");
  const cur = sel.value;
  const opts = ['<option value="AUTO">AUTO — try every rule</option>'];
  (HEALTH.rules || []).forEach((r) =>
    opts.push(`<option value="${esc(r.name)}">${esc(r.name)} (${r.strategy})</option>`));
  sel.innerHTML = opts.join("");
  if (cur) sel.value = cur;
}

$$(".seg-btn").forEach((b) => b.addEventListener("click", () => {
  $$(".seg-btn").forEach((x) => x.classList.remove("active"));
  b.classList.add("active");
  const file = b.dataset.mode === "file";
  $("#test-file-pane").style.display = file ? "" : "none";
  $("#test-paste-pane").style.display = file ? "none" : "";
}));

// file picker / dropzone
let PICKED_FILE = null;
const dz = $("#dropzone");
$("#test-file").addEventListener("change", (e) => setFile(e.target.files[0]));
dz.addEventListener("dragover", (e) => { e.preventDefault(); dz.classList.add("drag"); });
dz.addEventListener("dragleave", () => dz.classList.remove("drag"));
dz.addEventListener("drop", (e) => {
  e.preventDefault(); dz.classList.remove("drag");
  if (e.dataTransfer.files[0]) setFile(e.dataTransfer.files[0]);
});
function setFile(f) {
  PICKED_FILE = f;
  $("#dz-text").textContent = f ? `${f.name}  ·  ${(f.size / 1024).toFixed(1)} KB` : "Click to choose a log file, or drop it here";
}

$("#load-sample").addEventListener("click", () => {
  $("#test-text").value = SAMPLE_LINES;
  $$(".seg-btn").forEach((x) => x.classList.toggle("active", x.dataset.mode === "paste"));
  $("#test-file-pane").style.display = "none";
  $("#test-paste-pane").style.display = "";
  toast("Sample apache/nginx + auth lines loaded");
});

$("#run-test").addEventListener("click", runTest);
async function runTest() {
  const parser = $("#test-parser").value || "AUTO";
  const limit = parseInt($("#test-limit").value) || 20000;
  const fileMode = $("#test-file-pane").style.display !== "none";
  $("#test-hint").textContent = "running…";
  try {
    let result;
    if (fileMode) {
      if (!PICKED_FILE) { toast("Choose a file first", "bad"); $("#test-hint").textContent = ""; return; }
      const fd = new FormData();
      fd.append("file", PICKED_FILE);
      fd.append("parser", parser);
      fd.append("limit", limit);
      result = await api("/api/test", { method: "POST", body: fd });
    } else {
      const text = $("#test-text").value;
      if (!text.trim()) { toast("Paste some log lines first", "bad"); $("#test-hint").textContent = ""; return; }
      result = await api("/api/test", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ text, parser, limit }),
      });
    }
    renderTest(result);
    $("#test-hint").textContent = "";
  } catch (e) {
    $("#test-hint").textContent = "";
    toast(e.message, "bad");
  }
}

let LAST_EVENTS = [];
function renderTest(r) {
  $("#test-results").style.display = "";
  const s = r.stats;
  const cards = [
    { v: s.parsed_events, k: "Parsed events", cls: "good" },
    { v: s.match_rate + "%", k: "Match rate", cls: s.match_rate >= 80 ? "good" : s.match_rate >= 40 ? "warn" : "bad" },
    { v: s.no_match, k: "No match", cls: s.no_match ? "warn" : "" },
    { v: s.errors, k: "Errors", cls: s.errors ? "bad" : "" },
  ];
  $("#test-stats").innerHTML = cards.map((c) =>
    `<div class="stat ${c.cls}"><div class="v">${c.v}</div><div class="k">${c.k}</div></div>`).join("");

  LAST_EVENTS = r.events.map((e) => e.event);
  $("#evt-count").textContent = `(${r.events.length}${r.events_truncated ? " of " + s.parsed_events + ", capped" : ""})`;
  $("#test-events").innerHTML = r.events.length
    ? r.events.map((e) =>
        `<div class="event"><div class="event-h"><span class="ln">line ${e.line}</span>
         <span class="strat">${esc(e.rule)}</span></div>
         <pre>${jsonHighlight(e.event)}</pre></div>`).join("")
    : '<p class="muted small">No events parsed. Check the parser choice or the unparsed samples below.</p>';

  // unparsed samples
  const groups = [];
  ["no_match", "buffered", "errors"].forEach((g) => {
    (r.samples[g] || []).forEach((x) =>
      groups.push(`<div class="logline ${g === "errors" ? "ERROR" : "WARN"}"><span class="lvl">${g}</span>${esc(x.raw)}</div>`));
  });
  if (groups.length) {
    $("#test-samples-card").style.display = "";
    $("#test-samples").innerHTML = groups.join("");
  } else {
    $("#test-samples-card").style.display = "none";
  }

  if (!r.redis_ok && r.stats.buffered > 0) {
    toast("Stateful lines buffered — Redis not available, correlation disabled", "warn");
  }
  if (r.input_truncated) toast(`Only first ${r.input_limit} lines processed`, "warn");
}
$("#copy-events").addEventListener("click", () => {
  navigator.clipboard.writeText(JSON.stringify(LAST_EVENTS, null, 2))
    .then(() => toast("Events JSON copied", "good"));
});

/* ============================================================== *
 *  Rules
 * ============================================================== */
let RULES = [];
let CURRENT_RULE = null;
async function loadRules() {
  try {
    const d = await api("/api/rules");
    RULES = d.rules || [];
    $("#rule-items").innerHTML = RULES.map((r) =>
      `<div class="rule-row" data-file="${esc(r.file)}">
         <span class="nm">${esc(r.name)}</span>
         <span class="meta"><span class="strat">${r.strategy}</span><span>${r.fields} fields</span></span>
       </div>`).join("") || '<p class="muted small">No rules yet.</p>';
    $$("#rule-items .rule-row").forEach((row) =>
      row.addEventListener("click", () => openRule(row.dataset.file)));
  } catch (e) { toast(e.message, "bad"); }
}
async function openRule(file) {
  try {
    const d = await api("/api/rules/" + encodeURIComponent(file));
    CURRENT_RULE = file;
    $("#rule-filename").value = d.filename;
    $("#rule-content").value = d.content;
    $("#rule-ecs").innerHTML = "";
    $("#rule-msg").textContent = "";
    $$("#rule-items .rule-row").forEach((r) => r.classList.toggle("active", r.dataset.file === file));
  } catch (e) { toast(e.message, "bad"); }
}
$("#new-rule").addEventListener("click", () => {
  CURRENT_RULE = null;
  $("#rule-filename").value = "";
  $("#rule-content").value = "";
  $("#rule-ecs").innerHTML = "";
  $("#rule-msg").textContent = "New rule — pick a template, name the file, then Save.";
  $$("#rule-items .rule-row").forEach((r) => r.classList.remove("active"));
});
$("#rule-template").addEventListener("change", (e) => {
  const t = e.target.value;
  if (t && TEMPLATES[t]) {
    $("#rule-content").value = TEMPLATES[t];
    if (!$("#rule-filename").value) $("#rule-filename").value = t + "_custom.yaml";
  }
  e.target.value = "";
});
$("#save-rule").addEventListener("click", async () => {
  const filename = $("#rule-filename").value.trim();
  const content = $("#rule-content").value;
  if (!filename) { toast("Give the rule a file name (e.g. myparser.yaml)", "bad"); return; }
  try {
    const d = await api("/api/rules/save", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ filename, content }),
    });
    toast("Saved " + d.saved, "good");
    renderEcsReport(d.ecs);
    HEALTH = null;
    loadRules();
  } catch (e) { toast(e.message, "bad"); }
});
$("#check-rule").addEventListener("click", async () => {
  try {
    const d = await api("/api/ecs/check", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ content: $("#rule-content").value }),
    });
    renderEcsReport(d);
  } catch (e) { toast(e.message, "bad"); }
});
$("#delete-rule").addEventListener("click", async () => {
  const filename = $("#rule-filename").value.trim();
  if (!filename) return;
  if (!confirm(`Delete ${filename}? This cannot be undone.`)) return;
  try {
    await api("/api/rules/delete", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ filename }),
    });
    toast("Deleted " + filename, "good");
    $("#rule-filename").value = ""; $("#rule-content").value = ""; $("#rule-ecs").innerHTML = "";
    HEALTH = null; loadRules();
  } catch (e) { toast(e.message, "bad"); }
});
function renderEcsReport(ecs) {
  const out = [];
  if (ecs.problems && ecs.problems.length) {
    ecs.problems.forEach((p) =>
      out.push(`<div class="ecs-line bad">✗ <b>${esc(p.field)}</b> → use <b>${esc(p.fix)}</b> <span class="muted">(${esc(p.loc)})</span></div>`));
  }
  if (ecs.customs && ecs.customs.length) {
    ecs.customs.forEach((c) =>
      out.push(`<div class="ecs-line warn">~ ${esc(c.field)} <span class="muted">custom field, allowed${c.hint ? " · closest: " + esc(c.hint) : ""}</span></div>`));
  }
  if (!out.length || (!ecs.problems.length && !ecs.customs.length)) {
    out.unshift(`<div class="ecs-line good">✓ ${ecs.ok} field(s) valid ECS — nothing to fix</div>`);
  } else if (!ecs.problems.length) {
    out.unshift(`<div class="ecs-line good">✓ ${ecs.ok} valid ECS field(s), no errors</div>`);
  }
  $("#rule-ecs").innerHTML = out.join("");
}

/* ============================================================== *
 *  Config
 * ============================================================== */
async function loadConfig() {
  try {
    const d = await api("/api/config");
    $("#config-content").value = d.content || "";
    $("#config-msg").textContent = d.found ? d.path || "" : "config.yaml not found — saving will create it.";
  } catch (e) { toast(e.message, "bad"); }
}
$("#save-config").addEventListener("click", async () => {
  try {
    await api("/api/config/save", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ content: $("#config-content").value }),
    });
    toast("config.yaml saved", "good");
    HEALTH = null;
  } catch (e) { toast(e.message, "bad"); }
});
$("#validate-config").addEventListener("click", async () => {
  try {
    const d = await api("/api/config/validate", { method: "POST" });
    $("#config-report-card").style.display = "";
    $("#config-report").innerHTML = renderReport(d.lines);
    toast(d.summary.passed ? `Passed · ${d.summary.warnings} warnings`
      : `${d.summary.errors} errors · ${d.summary.warnings} warnings`,
      d.summary.passed ? "good" : "bad");
  } catch (e) { toast(e.message, "bad"); }
});
function renderReport(lines) {
  return lines.map((l) =>
    `<div class="logline ${l.level}">${l.level === "SECTION" ? "" : `<span class="lvl">${l.level}</span>`}${esc(l.message)}</div>`
  ).join("");
}

/* ============================================================== *
 *  ECS Helper
 * ============================================================== */
$("#ecs-classify").addEventListener("click", ecsClassify);
$("#ecs-field").addEventListener("keydown", (e) => { if (e.key === "Enter") ecsClassify(); });
async function ecsClassify() {
  const field = $("#ecs-field").value.trim();
  if (!field) return;
  try {
    const d = await api("/api/ecs/classify?field=" + encodeURIComponent(field));
    const map = {
      ecs: ["good", `✓ <b>${esc(field)}</b> is a valid ECS field`],
      alias: ["bad", `✗ <b>${esc(field)}</b> is not ECS — use <b>${esc(d.suggestion)}</b>`],
      typo: ["bad", `✗ <b>${esc(field)}</b> looks like a typo of <b>${esc(d.suggestion)}</b>`],
      custom: ["warn", `~ <b>${esc(field)}</b> is a custom field (allowed)${d.suggestion ? " · closest ECS: <b>" + esc(d.suggestion) + "</b>" : ""}`],
    };
    const [cls, msg] = map[d.status] || ["warn", "unknown"];
    let html = `<div class="ecs-line ${cls}">${msg}</div>`;
    if (d.suggestions && d.suggestions.length && d.status !== "ecs") {
      html += `<div class="ecs-line warn">candidates: ${d.suggestions.map(esc).join(", ")}</div>`;
    }
    $("#ecs-classify-out").innerHTML = html;
  } catch (e) { toast(e.message, "bad"); }
}
$("#ecs-find").addEventListener("click", ecsFind);
$("#ecs-search").addEventListener("keydown", (e) => { if (e.key === "Enter") ecsFind(); });
async function ecsFind() {
  const q = $("#ecs-search").value.trim();
  if (!q) return;
  try {
    const d = await api("/api/ecs/find?q=" + encodeURIComponent(q));
    $("#ecs-find-out").innerHTML = d.results.length
      ? d.results.map((f) => `<div class="ecs-line good">${esc(f)}</div>`).join("")
      : `<div class="ecs-line warn">No ECS field matches "${esc(q)}". If ECS has none, keep it as a custom field.</div>`;
  } catch (e) { toast(e.message, "bad"); }
}

/* ============================================================== *
 *  Preflight
 * ============================================================== */
$("#run-preflight").addEventListener("click", async () => {
  $("#pf-msg").textContent = "running…";
  try {
    const d = await api("/api/preflight", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        skip_live: !$("#pf-live").checked,
        timeout: parseFloat($("#pf-timeout").value) || 4,
      }),
    });
    $("#pf-report").innerHTML = renderReport(d.lines);
    $("#pf-msg").textContent = "";
    toast(d.passed ? "Preflight PASSED" : `Preflight: ${d.errors} error(s)`, d.passed ? "good" : "bad");
  } catch (e) { $("#pf-msg").textContent = ""; toast(e.message, "bad"); }
});

/* ---------- benchmark ---------- */
let BENCH_RUNNING = false;
async function runBench(mode, body, msgSel) {
  if (BENCH_RUNNING) { toast("A benchmark is already running", "bad"); return; }
  BENCH_RUNNING = true;
  $(msgSel).textContent = "running… this can take a while";
  const out = $("#bm-out");
  out.classList.add("muted");
  out.textContent = `running ${mode} benchmark…`;
  try {
    const d = await api("/api/benchmark/" + mode, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    out.textContent = d.output || "(no output)";
    out.classList.remove("muted");
    toast("Benchmark finished", "good");
  } catch (e) {
    out.textContent = "Error: " + e.message;
    toast(e.message, "bad");
  }
  $(msgSel).textContent = "";
  BENCH_RUNNING = false;
}
$("#bm-run-capacity").addEventListener("click", () =>
  runBench("capacity", { seconds: parseFloat($("#bm-seconds").value) || 1 }, "#bm-cap-msg"));
$("#bm-run-live").addEventListener("click", () =>
  runBench("live", { sample: parseInt($("#bm-sample").value, 10) || 500 }, "#bm-live-msg"));
$("#bm-run-history").addEventListener("click", () =>
  runBench("history", {
    index: $("#bm-index").value,
    days: parseInt($("#bm-days").value, 10) || 3,
    interval: $("#bm-interval").value,
    es: $("#bm-es").value,
    password: $("#bm-pass").value,
  }, "#bm-hist-msg"));

/* ---------- refresh ---------- */
$("#refresh-btn").addEventListener("click", () => {
  HEALTH = null;
  const active = $(".view.active").id.replace("view-", "");
  showView(active);
  toast("Refreshed");
});

/* ============================================================== *
 *  Rule templates + sample lines (embedded — works offline)
 * ============================================================== */
const TEMPLATES = {
  stateless: `pattern_name: "my_access"
strategy: "stateless"

# One regex with (?P<name>...) groups; first match wins.
regex: '(?P<ip>[\\d\\.]+) - - \\[(?P<timestamp>[^\\]]+)\\] "(?P<method>\\w+) (?P<path>[^\\s]+) HTTP/[\\d\\.]+" (?P<status>\\d+) (?P<body_bytes>\\d+)'

mapping:
  ip: "source.ip"
  method: "http.request.method"
  path: "url.path"
  status: "http.response.status_code|int"
  body_bytes: "http.response.body.bytes|int"

static:
  event.kind: "event"
  event.category: "web"
`,
  multi_match: `pattern_name: "my_auth"
strategy: "multi_match"

# Several regexes; the first one that matches a line wins.
patterns:
  - name: "ssh_failed"
    regex: 'Failed password for (?:invalid user )?(?P<user>\\S+) from (?P<ip>[\\d\\.]+)'
    mapping:
      user: "user.name"
      ip: "source.ip"
    static:
      event.action: "logon-failed"
      event.outcome: "failure"
  - name: "ssh_accepted"
    regex: 'Accepted password for (?P<user>\\S+) from (?P<ip>[\\d\\.]+)'
    mapping:
      user: "user.name"
      ip: "source.ip"
    static:
      event.action: "logon"
      event.outcome: "success"
`,
  stateful: `pattern_name: "my_mail"
strategy: "stateful"

# Multi-line events correlated by a transaction id (needs Redis at runtime).
id_regex: '(?P<id>[A-F0-9]{10,})'
end_signal: "removed"

patterns:
  - name: "client"
    regex: 'client=(?P<client>\\S+)\\[(?P<ip>[\\d\\.]+)\\]'
    mapping:
      ip: "source.ip"
  - name: "to"
    regex: 'to=<(?P<to>[^>]+)>'
    mapping:
      to: "email.to.address"

static:
  event.category: "email"
`,
  json_map: `pattern_name: "my_json"
strategy: "json_map"

# Dot-path mapping of JSON logs. Use '*' to walk a list.
mapping:
  src_ip: "source.ip"
  user.name: "user.name"
  action: "event.action"

static:
  event.kind: "event"
`,
  xml_xpath: `pattern_name: "my_xml"
strategy: "xml_xpath"

# Each matched element becomes one event.
items_xpath: ".//result"

mapping:
  host: "host.name"
  "nvt/@oid": "vulnerability.id"
  severity: "vulnerability.score.base|float"

static:
  event.category: "vulnerability"
`,
};

const SAMPLE_LINES = `192.168.1.50 - - [20/Jun/2026:10:11:12 +0000] "GET /index.html HTTP/1.1" 200 4523 "-" "Mozilla/5.0"
203.0.113.9 - - [20/Jun/2026:10:11:13 +0000] "POST /login HTTP/1.1" 401 120 "-" "curl/8.0"
8.8.8.8 - - [20/Jun/2026:10:11:14 +0000] "GET /admin HTTP/1.1" 403 0 "-" "sqlmap/1.7"`;

/* ============================================================== *
 *  Monitor (live ops) — polls /api/monitor, no page refresh
 * ============================================================== */
let MON_TIMER = null;
let EPS_HISTORY = [];
const EPS_MAX = 90;

function startMonitor() {
  pollMonitor();
  if (MON_TIMER) clearInterval(MON_TIMER);
  MON_TIMER = setInterval(() => { if ($("#mon-auto").checked) pollMonitor(); }, 2000);
}
function stopMonitor() {
  if (MON_TIMER) { clearInterval(MON_TIMER); MON_TIMER = null; }
}
$("#mon-refresh").addEventListener("click", pollMonitor);

function fmtBytes(b) {
  if (b == null) return "—";
  const u = ["B", "KB", "MB", "GB", "TB"];
  let i = 0; let v = b;
  while (v >= 1024 && i < u.length - 1) { v /= 1024; i++; }
  return v.toFixed(v < 10 && i > 0 ? 1 : 0) + " " + u[i];
}
function fmtNum(n) { return (n == null ? "—" : Number(n).toLocaleString()); }
function fmtDur(s) {
  if (s == null) return "—";
  s = Math.floor(s);
  const d = Math.floor(s / 86400); s -= d * 86400;
  const h = Math.floor(s / 3600); s -= h * 3600;
  const m = Math.floor(s / 60); s -= m * 60;
  if (d) return `${d}d ${h}h ${m}m`;
  if (h) return `${h}h ${m}m`;
  if (m) return `${m}m ${s}s`;
  return `${s}s`;
}

async function pollMonitor() {
  try {
    const m = await api("/api/monitor");
    renderMonitor(m);
  } catch (e) {
    $("#mon-state").textContent = "Monitor error";
    $("#mon-substate").textContent = e.message;
  }
}

function renderMonitor(m) {
  const STATE = {
    running: ["running", "Engine running", "good"],
    starting: ["starting", "Engine starting / no fresh stats", "warn"],
    stopped: ["stopped", "Engine stopped", "bad"],
  };
  const [cls, label] = STATE[m.status] || STATE.stopped;
  $("#mon-dot").className = "big-dot " + cls;
  $("#mon-state").textContent = label;
  $("#nav-live-dot").className = "live-dot " + (m.status === "running" ? "on" : m.status === "stopped" ? "bad" : "");

  const sub = [];
  if (m.role) sub.push(m.role);
  if (m.stats_age_sec != null) sub.push(`stats ${m.stats_age_sec}s ago`);
  if (!m.stats_fresh && m.running) sub.push("(stale — is it receiving logs?)");
  $("#mon-substate").textContent = sub.join("  ·  ") || "—";

  // meta line
  const meta = [];
  meta.push(`<span>Uptime <b>${fmtDur(m.uptime_sec)}</b></span>`);
  meta.push(`<span>Workers <b>${m.workers_alive}/${m.workers}</b></span>`);
  if (m.kafka && m.kafka.input_topic) meta.push(`<span>Topic <b>${esc(m.kafka.input_topic)}</b></span>`);
  if (m.kafka && m.kafka.group_id) meta.push(`<span>Group <b>${esc(m.kafka.group_id)}</b></span>`);
  if (m.pids && m.pids.length) meta.push(`<span>PID <b>${m.pids.join(", ")}</b></span>`);
  $("#mon-meta").innerHTML = meta.join("");

  // control buttons
  const cc = $("#mon-control");
  if (m.control_enabled) {
    cc.style.display = "";
    cc.innerHTML =
      `<span class="muted small">Engine service control:</span>
       <button class="btn ghost small" data-act="start">Start</button>
       <button class="btn ghost small" data-act="restart">Restart</button>
       <button class="btn danger small" data-act="stop">Stop</button>
       <span class="muted small" id="mon-ctl-msg"></span>`;
    cc.querySelectorAll("[data-act]").forEach((b) =>
      b.addEventListener("click", () => engineControl(b.dataset.act)));
  } else {
    cc.style.display = "";
    cc.innerHTML = `<span class="muted small">▸ Start/stop/restart from here is off. Launch the UI with
      <code>SOC_UI_ALLOW_CONTROL=1</code> (Linux/systemd) to enable it.</span>`;
  }

  // stat cards
  const cards = [
    { v: fmtNum(m.eps), k: "Events / sec", cls: m.eps > 0 ? "good" : "" },
    { v: fmtNum(m.total_processed), k: "Total processed", cls: "acc" },
    { v: fmtNum(m.total_errors), k: "Total errors", cls: m.total_errors > 0 ? "warn" : "" },
    { v: fmtBytes(m.engine_rss), k: "Engine RAM", cls: "" },
  ];
  $("#mon-stats").innerHTML = cards.map((c) =>
    `<div class="stat ${c.cls}"><div class="v">${c.v}</div><div class="k">${c.k}</div></div>`).join("");

  // sparkline history (only while running)
  EPS_HISTORY.push(m.status === "running" ? (m.eps || 0) : 0);
  if (EPS_HISTORY.length > EPS_MAX) EPS_HISTORY.shift();
  drawSpark();
  $("#mon-eps-now").textContent = `now ${fmtNum(m.eps)} eps · peak ${fmtNum(Math.max(...EPS_HISTORY).toFixed(0))}`;

  renderSys(m.system, m.engine_rss);
  renderRuleTable(m.parser_stats);
  renderWorkerTable(m.workers_detail);

  const u = new Date();
  $("#mon-updated").textContent = "updated " + u.toLocaleTimeString();
}

function drawSpark() {
  const cv = $("#mon-spark");
  if (!cv) return;
  const w = cv.clientWidth || 600, h = cv.height;
  cv.width = w;
  const ctx = cv.getContext("2d");
  ctx.clearRect(0, 0, w, h);
  const data = EPS_HISTORY;
  if (data.length < 2) return;
  const max = Math.max(...data, 1);
  const stepX = w / (EPS_MAX - 1);
  const y = (v) => h - 6 - (v / max) * (h - 14);
  const x = (i) => i * stepX;
  // area
  const grad = ctx.createLinearGradient(0, 0, 0, h);
  grad.addColorStop(0, "rgba(99,102,241,0.30)");
  grad.addColorStop(1, "rgba(99,102,241,0.02)");
  ctx.beginPath();
  ctx.moveTo(x(0), h);
  data.forEach((v, i) => ctx.lineTo(x(i), y(v)));
  ctx.lineTo(x(data.length - 1), h);
  ctx.closePath();
  ctx.fillStyle = grad;
  ctx.fill();
  // line
  ctx.beginPath();
  data.forEach((v, i) => (i ? ctx.lineTo(x(i), y(v)) : ctx.moveTo(x(i), y(v))));
  ctx.strokeStyle = "#5b6fd8";
  ctx.lineWidth = 2;
  ctx.lineJoin = "round";
  ctx.stroke();
}

function meter(label, value, sub, percent, hot) {
  const pct = percent == null ? 0 : Math.min(100, Math.max(0, percent));
  return `<div class="meter-row">
    <div class="meter-top"><span>${label}</span><span class="mv">${sub}</span></div>
    <div class="meter ${hot ? "hot" : ""}"><span style="width:${pct}%"></span></div>
  </div>`;
}
function renderSys(s, engineRss) {
  if (!s) { $("#mon-sys").innerHTML = '<p class="muted small">No system metrics available.</p>'; return; }
  const parts = [];
  if (s.cpu_percent != null) {
    parts.push(meter("CPU", s.cpu_percent, `${s.cpu_percent}%`, s.cpu_percent, s.cpu_percent > 85));
  }
  if (s.mem) {
    parts.push(meter("Memory", s.mem.percent,
      `${fmtBytes(s.mem.used)} / ${fmtBytes(s.mem.total)} · ${s.mem.percent}%`,
      s.mem.percent, s.mem.percent > 88));
  }
  const mini = [];
  if (s.cpu_count != null) mini.push(`<span>Cores <b>${s.cpu_count}</b></span>`);
  if (s.load) mini.push(`<span>Load <b>${s.load.join(" / ")}</b></span>`);
  mini.push(`<span>Engine RAM <b>${fmtBytes(engineRss)}</b></span>`);
  mini.push(`<span>Host <b>${esc(s.platform || "")}</b></span>`);
  parts.push(`<div class="sys-mini">${mini.join("")}</div>`);
  if (s.cpu_percent == null && !s.mem) {
    parts.unshift('<p class="muted small">CPU/RAM detail needs Linux (/proc) or the optional psutil package.</p>');
  }
  $("#mon-sys").innerHTML = parts.join("");
}

function renderRuleTable(ps) {
  const rows = Object.entries(ps || {}).map(([name, s]) => ({
    name, ev: s.parsed_events || 0, nm: s.no_match || 0, bf: s.buffered || 0,
    ex: s.expired || 0,
    er: (s.errors || 0) + (s.redis_errors || 0),
  })).sort((a, b) => b.ev - a.ev);
  $("#mon-rule-count").textContent = `(${rows.length})`;
  const body = rows.map((r) =>
    `<tr><td class="rname">${esc(r.name)}</td>
      <td class="num ok">${fmtNum(r.ev)}</td>
      <td class="num">${fmtNum(r.nm)}</td>
      <td class="num">${fmtNum(r.bf)}</td>
      <td class="num ${r.ex ? "warn" : ""}">${fmtNum(r.ex)}</td>
      <td class="num ${r.er ? "er" : ""}">${fmtNum(r.er)}</td></tr>`).join("");
  $("#mon-rules tbody").innerHTML = body ||
    '<tr><td colspan="6" class="muted small">No parser activity yet (engine idle or not running).</td></tr>';
}

function renderWorkerTable(ws) {
  ws = ws || [];
  $("#mon-worker-count").textContent = `(${ws.length})`;
  const body = ws.map((w) =>
    `<tr><td>${w.worker_id == null ? "—" : "w" + w.worker_id}</td>
      <td class="num">${w.pid || "—"}</td>
      <td class="num ok">${fmtNum(w.eps)}</td>
      <td class="num">${fmtNum(w.total_processed)}</td>
      <td class="num">${fmtDur(w.uptime_sec)}</td>
      <td><span class="${w.alive === false ? "dot-dead" : "dot-alive"}"></span></td></tr>`).join("");
  $("#mon-workers tbody").innerHTML = body ||
    '<tr><td colspan="6" class="muted small">No workers reporting.</td></tr>';
}

async function engineControl(act) {
  const msg = $("#mon-ctl-msg");
  if (msg) msg.textContent = act + "…";
  try {
    const d = await api("/api/engine/" + act, { method: "POST" });
    toast(`${act}: ${d.ok ? "ok" : "failed"} ${d.output ? "— " + d.output : ""}`, d.ok ? "good" : "bad");
    if (msg) msg.textContent = d.output || "";
    setTimeout(pollMonitor, 1200);
  } catch (e) {
    toast(e.message, "bad");
    if (msg) msg.textContent = e.message;
  }
}

$("#mon-dlq-load").addEventListener("click", async () => {
  try {
    const d = await api("/api/monitor/dlq?n=25");
    if (!d.entries.length) { $("#mon-dlq").innerHTML = '<p class="muted small">DLQ is empty — nothing unparsed. 🎉</p>'; return; }
    $("#mon-dlq").innerHTML = d.entries.reverse().map((e) =>
      `<div class="logline ERROR"><span class="lvl">${esc(e.program || "?")}</span>${esc(e.error || "")} — ${esc((e.raw || "").slice(0, 200))}</div>`
    ).join("");
  } catch (e) { toast(e.message, "bad"); }
});

/* ============================================================== *
 *  Session (whoami / logout)
 * ============================================================== */
async function loadSession() {
  try {
    const w = await api("/api/whoami");
    if (w.user) {
      $("#user-name").textContent = w.user;
      $("#user-chip").style.display = "";
    }
    if (!w.auth_disabled) $("#logout-btn").style.display = "";
  } catch (e) { /* 401 already redirects */ }
}
$("#logout-btn").addEventListener("click", async () => {
  try { await api("/api/logout", { method: "POST" }); } catch (e) {}
  window.location.href = "/login";
});

/* ---------- boot ---------- */
loadSession();
loadHealth();
showView("dashboard");
