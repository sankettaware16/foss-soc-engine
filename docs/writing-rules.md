# Writing Parsers (Rules) — the ECS-friendly guide

A **rule** is a small YAML file in [`rules/`](../rules/) that teaches the engine how
to turn one kind of raw log into clean, structured JSON. You do **not** edit any
Python to add a parser — you just add a YAML file.

> **The one golden rule:** every field you produce must be an **ECS field**
> (Elastic Common Schema). You don't need to memorise ECS — the built-in helper
> tells you the right field name as you go (think spell-check for log fields).

---

## 1. Quick start (5 steps)

1. Create a file in `rules/`, e.g. `rules/myapp.yaml`.
2. Give it a unique `pattern_name` and pick a `strategy` (see §3).
3. Write the `mapping` — turn each captured value into an **ECS field**.
4. **Check it** with the helper (this is the important part):
   ```bash
   python3 ecs_helper.py check rules/myapp.yaml
   ```
   Fix anything marked `x` (it shows you the correct ECS field). Custom fields
   marked `~` are fine.
5. Point your log source at the rule in `config.yaml` → `program_mapping`, then
   restart: `sudo systemctl restart foss-soc`.

That's it. The same `ecs_helper.py` is also run automatically by the deploy
validator (`python3 test_config.py`), so a rule with bad field names cannot ship.

---

## 2. The helper (your ECS autocorrect)

You will not know every ECS field — nobody does. Use the helper instead.

```bash
# Check a rule (or the whole folder). Tells you which fields are wrong + the fix.
python3 ecs_helper.py check rules/myapp.yaml
python3 ecs_helper.py check rules/

# Auto-apply the safe corrections (e.g. srcip -> source.ip, status -> http.response.status_code)
python3 ecs_helper.py fix rules/myapp.yaml

# "What's the ECS field for ___?"  Search by plain words.
python3 ecs_helper.py find "country"
python3 ecs_helper.py find "http status"
python3 ecs_helper.py find "user name"

# Interactive: type a field or a concept, get the right ECS field.
python3 ecs_helper.py
```

What the symbols mean in `check`:

| Symbol | Meaning | Action |
|---|---|---|
| `OK`  | all fields are valid ECS | nothing to do |
| `x`   | not ECS, but ECS **has** a field for it | change it to the suggested field |
| `~`   | custom field — ECS has **no** field for this | allowed, keep it |

---

## 3. Choosing a strategy

Set `strategy:` to one of these. Pick by the **shape** of your log.

| Strategy | Use it when the log is… | Example sources |
|---|---|---|
| `stateless`  | one line, one consistent format | Apache/Nginx access, IDS alerts |
| `multi_match`| one source, several line formats | Linux auth (ssh/sudo/su), app logs |
| `stateful`   | one event spread over several lines, tied by an ID | Postfix mail flow, WAF transactions |
| `json_map`   | already JSON | ModSecurity, cloud audit, app JSON |
| `xml_xpath`  | XML | OpenVAS/Nessus scan exports |

### `stateless` — one regex
```yaml
pattern_name: "myapp_access"
strategy: "stateless"
regex: '(?P<ip>[\d.]+) \[(?P<ts>[^\]]+)\] "(?P<method>\w+) (?P<path>\S+)" (?P<status>\d+)'
timestamp:            # where the event's REAL time lives (see §5)
  group: ts
  format: clf
mapping:
  ip: "source.ip"
  method: "http.request.method"
  path: "url.path"
  status: "http.response.status_code|int"
static:
  event.category: "web"
  event.type: "access"
```

### `multi_match` — several regexes, first match wins
```yaml
pattern_name: "myapp_auth"
strategy: "multi_match"
patterns:
  - name: "login_ok"
    prematch: "Accepted password"   # cheap substring gate BEFORE the regex
    regex: 'Accepted password for (?P<user>\w+) from (?P<ip>[\d.]+)'
    mapping: { user: "user.name", ip: "source.ip" }
    static:  { event.action: "login", event.outcome: "success" }
  - name: "login_fail"
    prematch: "Failed password"
    regex: 'Failed password for (?:invalid user )?(?P<user>\w+) from (?P<ip>[\d.]+)'
    mapping: { user: "user.name", ip: "source.ip" }
    static:  { event.action: "login", event.outcome: "failure" }
```

**`prematch:` — keep big rules fast.** A plain substring (or a list = any-of)
that must appear in the line before the pattern's regex is even tried. A
substring check costs a fraction of a failed regex, so with many patterns
almost all of them get skipped instantly — measured: a 500-pattern rule runs
**~13× faster** with prematch. Rules stay correct without it (it's purely an
optimization) — but add it to every pattern you write:
- pick a substring that is ALWAYS present when the regex matches
  (`"Failed password"`, `"client="`, `"NOQUEUE"`);
- it is case-sensitive plain text, NOT a regex;
- also works per-pattern in `stateful` rules (where it pays off most — every
  transaction line is checked against ALL patterns) and at the top level of
  any rule to gate the whole rule.

### `stateful` — stitch multiple lines by a transaction ID
```yaml
pattern_name: "myapp_txn"
strategy: "stateful"
id_regex: '(?P<id>[A-Z0-9]{10,12}):'   # MUST capture a group named 'id'
end_signal: "removed"                   # when this text appears, the event is emitted
state_ttl_sec: 300                      # optional: max transaction lifetime (default 300)
patterns:
  - regex: 'client=(?P<host>\S+)\[(?P<ip>[\d.]+)\]'
    mapping: { ip: "source.ip" }
  - regex: 'from=<(?P<sender>[^>]+)>'
    mapping: { sender: "email.from.address" }
```

Good to know about stateful rules:
- A transaction that never sees its `end_signal` within `state_ttl_sec` is **not
  lost**: the engine emits whatever it collected, tagged `event.incomplete: true`
  and `event.reason: "transaction_timeout"`, and counts it in the Monitor's
  **Expired** column. Raise `state_ttl_sec` for sources whose transactions
  legitimately run long.
- Lines *without* a transaction ID still parse: the first matching pattern wins
  (that's how postfix handles `connect from`, SASL failures, warnings…). Put
  specific patterns first and any catch-all last.

### `json_map` — map JSON paths (dot notation, `*` = every item in a list)
```yaml
pattern_name: "myapp_json"
strategy: "json_map"
mapping:
  transaction.client_ip: "source.ip"
  transaction.request.method: "http.request.method"
  transaction.messages.*.details.ruleId: "rule.id"   # * walks a list
static:
  event.kind: "alert"
  event.category: "web"
```

### `xml_xpath` — map XML elements/attributes
```yaml
pattern_name: "myapp_scan"
strategy: "xml_xpath"
items_xpath: ".//result"          # repeat one event per matching element
mapping:
  nvt/@oid: "vulnerability.id"     # /@attr reads an attribute
  host: "destination.ip"
  severity: "vulnerability.severity|float"
static:
  event.category: "vulnerability"
```

---

## 4. Mapping syntax cheatsheet

- **Left = where it came from, Right = ECS field.**
  `ip: "source.ip"` means "the capture group `ip` goes into ECS `source.ip`".
- **Types:** add `|int` or `|float` to the ECS target to convert numbers:
  `status: "http.response.status_code|int"`. Works in **every** strategy
  (regex, json_map, xml_xpath); coercion is safe — a non-numeric value stays a
  string and the event survives. These two suffixes are the **only** value
  transforms the engine has (no lookups, no string ops) — anything fancier
  belongs downstream (Logstash / ES ingest pipeline).
- **Nesting** is automatic from dots: `source.geo.country_name` builds the nested
  object for you.
- **JSON paths** use dots; `*` expands a list: `items.*.id`.
- **XML** uses element paths; `tag/@attr` reads an attribute.
- **`static:`** adds fixed fields to every event (the keys are ECS fields too).
- Repeated mappings to the **same** field automatically become a list.

### Site-tunable values — the `vars:` block (optional)

If a regex needs a value that differs per deployment (YOUR mail domain, YOUR
hostname prefix), don't hardcode it — declare it once at the top and reference
it with `%{name}`:

```yaml
vars:
  internal_domains: ["example.com", "example.org"]   # EDIT for your site

patterns:
  - prematch: "from=<"
    regex: 'from=<(?P<sender>[^@]+@(?P<s_domain>%{internal_domains}))>'
```

- **List values** are regex-escaped and joined automatically into
  `(?:example\.com|example\.org)` — operators write plain domains, no regex
  knowledge needed. **String values** are inserted verbatim (for authors who
  want a regex fragment).
- A `%{token}` with no matching var is a load **error** (the rule is disabled
  and `test_config.py` fails) — a typo can never silently break matching.
- The shipped `rules/postfix.yaml` uses this for its internal/external mail
  classification: edit its one `internal_domains` line for your site.

---

## 5. Event time — the `timestamp:` block (add it to every rule)

The engine writes three time fields on every event:

| Field | Meaning |
|---|---|
| `@timestamp` | the event's **real** time, parsed from the log line — this is what Kibana's time picker and Elasticsearch index routing use |
| `event.ingested` | when the engine parsed the line (wall clock, always set) |
| `event.timestamp_source` | `log` = parsed from the line · `log_assumed_utc` = parsed, but the log carried no timezone so UTC was assumed · `ingest_fallback` = couldn't parse (or no `timestamp:` block), so `@timestamp` = `event.ingested` |

**Why you care:** without a `timestamp:` block, a delayed batch of logs (a Kafka
backlog drained after an outage, a scanner report uploaded hours later) gets
stamped with *today's* date and lands on the wrong day in Elasticsearch.

Declare where the time lives and its format:

```yaml
timestamp:
  group: ts               # a named group your regex captures, OR…
  # field: info.start     # …a dot-path (json_map) / element path (xml_xpath), OR…
  # regex: '^(?P<ts>\S+)' # …an independent regex run on the raw line (name the group `ts`)
  format: clf             # see the table below
  tz: "+05:30"            # ONLY for formats with no zone, if you know the source's zone
```

| `format:` | Log looks like | Typical source |
|---|---|---|
| `clf` | `09/Jul/2026:13:31:48 +0530` (12-hour `01:31:48 PM` also OK) | Apache/Nginx access |
| `iso8601` | `2026-07-09T13:31:48.123456+05:30`, `…Z` | rsyslog RFC5424, JSON logs |
| `rfc3164` | `Jul  9 13:31:48` (no year, no zone) | classic syslog (auth.log) |
| `epoch` | `1594282308` (seconds/ms/µs auto-detected) | Nessus, JSON APIs |
| `suricata` | `07/09/2023-13:31:48.123456` | Suricata fast.log |
| `nginx_error` | `2026/07/09 13:31:48` | Nginx error log |
| `asctime` | `Tue Jul  9 13:31:48 2026` | ModSecurity |
| `roundcube` | `09-Jul-2026 13:31:48 +0530` | Roundcube |
| any `%` string | explicit Python strptime, e.g. `"%m/%d/%Y %H:%M:%S"` | anything else |

Good to know:

- Everything is normalized to **UTC**; the output string shape never changes.
- `multi_match` / `stateful`: a top-level `timestamp:` applies to all patterns;
  a per-pattern `timestamp:` overrides it (see `rules/nginx.yaml`). Stateful
  events are stamped from the transaction's **first** line.
- `rfc3164` has no year: the engine assumes the current year unless that puts
  the event more than 48 h in the future, then it uses the previous year (so
  December logs read in January stay in December).
- Ambiguous zone abbreviations (`IST`, `EST`, `CET`, …) are **refused on
  purpose** (IST alone can mean India, Israel or Ireland). Use a numeric
  `tz: "+05:30"` instead. Only `Z`/`UTC`/`GMT` and numeric offsets are accepted.
- A parse failure never drops the event: `@timestamp` stays at ingest time and
  `event.timestamp_source: ingest_fallback` makes it easy to count/find in
  Kibana. Filter on that field to spot sources that need a `timestamp:` fix.
- Regression suite: `python3 test_timestamps.py` (run it after touching any
  `timestamp:` block or `core/timeparse.py`).

---

## 6. The most common ECS fields (starter cheat-sheet)

Don't guess — but here are the ones you'll use most. For anything else, run
`python3 ecs_helper.py find "<thing>"`.

| You have… | Use ECS field |
|---|---|
| client/source IP, port | `source.ip`, `source.port` |
| server/target IP, port | `destination.ip`, `destination.port` |
| username | `user.name` |
| hostname of the sensor/log host | `host.name` |
| HTTP method / status / path / query | `http.request.method`, `http.response.status_code`, `url.path`, `url.query` |
| user agent | `user_agent.original` |
| bytes sent | `http.response.body.bytes` |
| what happened / result | `event.action`, `event.outcome` (`success`/`failure`), `event.reason` |
| category / type | `event.category`, `event.type`, `event.kind` |
| severity / log level | `event.severity`, `log.level` |
| file path / owner / mode | `file.path`, `file.owner`, `file.mode`, `file.uid` |
| process id / command | `process.pid`, `process.command_line` |
| TLS version / cipher | `tls.version`, `tls.cipher` |
| email from / to | `email.from.address`, `email.to.address` |
| CVE id / CVSS score | `vulnerability.id`, `vulnerability.score.base` |
| rule id / name | `rule.id`, `rule.name` |
| geo (auto-added by the engine) | `source.geo.country_name`, `source.geo.location` |
| ASN / IP owner (auto-added by the engine) | `source.as.number`, `source.as.organization.name` |

**When ECS truly has no field for your value**, keep your own — but put it under a
clear custom namespace named after your product (e.g. `myapp.session_token`), not
inside an ECS set. The helper will mark it `~ custom field, allowed`.

---

## 7. Test your rule

```bash
# Interactive: paste one raw line, see the parsed JSON
python3 test_rules.py

# Whole file: counts parsed vs unparsed, shows samples
python3 test_file.py /path/to/logs.txt myapp_access
python3 test_file.py /path/to/logs.txt AUTO          # let it pick the best rule

# ECS compliance
python3 ecs_helper.py check rules/myapp.yaml
```

### Golden samples — every rule ships its own exam (required for new rules)

Each rule has a tiny corpus under `tests/samples/<pattern_name>/`:

```
tests/samples/myapp_access/input.log        <- 5-20 real raw log lines
tests/samples/myapp_access/expected.ndjson  <- exactly what the engine must produce
```

Workflow when you add or change a rule:

```bash
# 1. put representative raw lines (incl. one that must NOT match) in input.log
# 2. generate/refresh the expected answers and REVIEW them:
python3 test_golden.py --update myapp_access
# 3. from now on, every change is checked automatically:
python3 test_golden.py                     # all rules; exits non-zero + diff on any change
```

CI runs `test_golden.py` on every push/PR, so an edit that silently changes any
rule's output for any sample line is rejected with a diff. That is what makes it
safe to accept rule contributions. (Timestamps and geo/ASN fields are normalized
out — they're covered by `test_timestamps.py` / `test_enrichment.py`; stateful
rules work in the exam via a built-in in-memory Redis stand-in.)

## 8. Hook your log source to the rule

In `config.yaml`, map the **source program name** (what your shipper sets) to the
rule's `pattern_name`:
```yaml
program_mapping:
  myapp_prod: "myapp_access"
  myapp_stage: "myapp_access"   # many sources can reuse one rule
```

**One messy source, several rules? Use a CHAIN.** If one server tag emits
several log shapes (web access + app errors + auth), map it to a *list* — the
engine tries the rules in order and the first one that handles a line wins
(handling = produced an event, or buffered it into a stateful transaction):

```yaml
program_mapping:
  webserver01: ["nginx_access", "php_errors", "linux_auth"]
```

Put the highest-volume rule first (its prematch/regex is tried first). Lines
no rule in the chain handles go to the DLQ under the first rule's name.

## 9. Common mistakes

- Using `srcip`, `status`, `useragent`, `cve` → run `ecs_helper.py fix` (they map to
  `source.ip`, `http.response.status_code`, `user_agent.original`, `vulnerability.id`).
- Forgetting `|int` on numbers (they stay strings without it).
- `stateful` rules **must** capture a group named `id` in `id_regex`.
- Putting custom fields inside ECS sets (e.g. `event.my_thing`) — use your own
  namespace instead.
- **Forgetting the `timestamp:` block** (§5) — every event gets stamped with the
  parse-time clock instead of the event's real time, so delayed logs land on the
  wrong day. The tell: `event.timestamp_source: "ingest_fallback"` in your events.
- Using a zone abbreviation like `tz: "IST"` — ambiguous abbreviations are refused;
  use a numeric offset (`tz: "+05:30"`).
- Mapping a captured time straight to `@timestamp` in `mapping:` — it goes in as a
  raw unparsed string. Use the `timestamp:` block instead so it's parsed and
  normalized to UTC.

---

## 10. The lazy way — let an AI write the rule for you

Don't want to write regex or learn ECS? Paste the **master prompt** below into any
AI chatbot, then paste a few **raw log lines**
underneath. It will hand you a finished, ECS-compliant YAML rule you can drop
straight into `rules/`.

After you paste the AI's YAML into a file, always run:
```bash
python3 ecs_helper.py check rules/your_new_rule.yaml
```
to confirm it's clean before deploying.

### 📋 Master prompt (copy everything in the box)

````text
You are an expert log-parsing engineer for the "TLSOC Engine". Your job: read
the RAW LOG SAMPLES I paste at the end and output ONE ready-to-use YAML parser
rule for this engine. Output ONLY the YAML inside a single code block — no
explanation before or after.

# OUTPUT FORMAT
Produce a YAML file with these keys:
- pattern_name: a short unique snake_case name for this log source (string)
- strategy: one of stateless | multi_match | stateful | json_map | xml_xpath
- the strategy-specific keys (below)
- timestamp: where the event's REAL time lives in the log and how to parse it
  (see TIMESTAMP RULES). Include it whenever the log carries a time — almost always.
- vars: (optional) site-tunable values used inside regexes (see VARS RULES)
- mapping: maps each captured value to an ECS field (see ECS RULES)
- static: (optional) fixed ECS fields added to every event

Choose the strategy by the shape of the logs:
- stateless  : single-line, one consistent format. Keys: regex, mapping, static.
- multi_match: one source with several line formats. Keys: patterns: a list of
               {name, prematch, regex, mapping, static}. Order matters; first
               match wins — put SPECIFIC patterns first and any generic
               catch-all LAST, or the catch-all swallows everything.
- stateful   : one event spread over multiple lines sharing a transaction id.
               Keys: id_regex (MUST contain a named group `id`), end_signal
               (a substring that marks the final line), patterns: list of
               {prematch, regex, mapping, static}. Optional state_ttl_sec
               (default 300): raise it for sources whose transactions
               legitimately run long (deferred mail, slow scans) — expired
               transactions are still emitted, tagged event.incomplete: true.
               Lines WITHOUT the id fall back to the first matching pattern as
               standalone events, so also cover the source's warning/error
               lines that carry no transaction id.
- json_map   : logs are JSON. mapping keys are dot-paths into the JSON; use `*`
               to walk every element of a list (e.g. items.*.id). Keys: mapping, static.
- xml_xpath  : logs are XML. Keys: items_xpath (element repeated per event),
               mapping where keys are element paths or `tag/@attr` for attributes.

# REGEX RULES
- Use Python `re` syntax with NAMED groups: (?P<name>...).
- Make patterns specific; escape literals; prefer [^"]* / \S+ over greedy .*.
- ROBUSTNESS: real logs are messier than the samples. Use \s+ between tokens
  instead of a single literal space wherever the producer might vary (URLs with
  trailing spaces produce double spaces; columns are often width-padded). Make
  trailing optional fields genuinely optional with (?: ...)?.
- The group name (left side of mapping) is arbitrary; the ECS field (right side)
  must be valid ECS.
- PERFORMANCE: give every multi_match/stateful pattern a `prematch:` — a plain
  case-sensitive substring (NOT a regex) that is always present in lines the
  regex matches (e.g. prematch: "Failed password"). The engine checks it with
  a cheap `in` before running the regex; this is what keeps rules with many
  patterns fast. A list means any-of: prematch: ["timeout", "timed out"].
  A top-level prematch: (next to strategy:) gates the WHOLE rule the same way.

# VARS RULES (optional — site-tunable values)
Values an operator must edit per site (internal domains, subnets, hostnames)
go in a top-level `vars:` block and are referenced inside regexes as %{name}.
Two behaviors, choose deliberately:
- LIST value  -> each entry is regex-ESCAPED and joined into (?:a\.com|b\.org):
  operators write plain literals, no regex knowledge needed.
    vars: { internal_domains: ["example.com", "example.org"] }
- STRING value -> inserted as a RAW regex fragment (single-quote it in YAML so
  backslashes survive). Use this when the value needs regex power, e.g. to
  match a domain AND all its subdomains:
    vars: { internal_domains: '(?:[\w-]+\.)*example\.com' }
Put a loud "EDIT THIS for your site" comment above the block.

# TIMESTAMP RULES (critical — this drives Elasticsearch index routing)
The engine fills @timestamp from the `timestamp:` block. Without it, events are
stamped with parse-time instead of the event's real time and delayed logs land on
the wrong day. Whenever the log lines contain a date/time, capture it and declare:
  timestamp:
    group: <named regex group>   # regex strategies: capture the time in the pattern
    # or field: <dot.path>       # json_map: dot-path / xml_xpath: element path
    # or regex: '^(?P<ts>...)'   # independent regex on the raw line (e.g. syslog prefix)
    format: <see below>
    tz: "+05:30"                 # ONLY if the format has no zone AND the zone is known
format must be one of these named formats (match by the sample's shape) or an
explicit Python strptime string:
  clf         -> 09/Jul/2026:13:31:48 +0530     (apache/nginx access; AM/PM ok)
  iso8601     -> 2026-07-09T13:31:48.123456+05:30 or ...Z (also RFC5424 syslog)
  rfc3164     -> Jul  9 13:31:48                (classic syslog: no year, no zone)
  epoch       -> 1594282308                     (Unix seconds/ms/us, auto-detected)
  suricata    -> 07/09/2023-13:31:48.123456
  nginx_error -> 2026/07/09 13:31:48
  asctime     -> Tue Jul  9 13:31:48 2026       (ModSecurity)
  roundcube   -> 09-Jul-2026 13:31:48 +0530
For multi_match/stateful put one timestamp block at the TOP level; add a
per-pattern timestamp block only for patterns with a different time format.
Never map a time into "@timestamp" or "event.created" via mapping as a substitute
for this block — mapped values are raw unparsed strings.
Never emit tz as an abbreviation (IST/EST/CET are ambiguous and rejected);
use a numeric offset like "+05:30". If the samples show no timezone and none is
known, omit tz (the engine assumes UTC and tags the event log_assumed_utc).

# ECS RULES (critical)
Every value on the RIGHT side of mapping, and every key under static, MUST be a
valid Elastic Common Schema (ECS) field. Add `|int` or `|float` to the ECS field
to coerce numbers, e.g. "http.response.status_code|int".
AMBIGUOUS ENDPOINTS: if a captured endpoint is sometimes an IP and sometimes a
hostname (proxy destinations, relay targets), map it to source.address or
destination.address — the engine classifies it into .ip or .domain per event
automatically (and geo/ASN-enriches IPs on BOTH the source and destination
side). Never force such a value into .ip or .domain yourself.
ORIGINAL TIME: alongside the timestamp block, also map the captured raw time
string to "event.original_time" — it is kept verbatim for audit/debugging.
Use these common ECS fields where they fit:
  source.ip, source.port, source.domain, source.address, source.bytes,
  destination.ip, destination.port, destination.domain, destination.address,
  host.name, user.name, user_agent.original, authentication.method,
  network.bytes, event.duration,
  http.request.method, http.response.status_code, http.request.referrer,
  http.response.body.bytes, url.path, url.query, url.domain,
  event.action, event.category, event.type, event.kind, event.outcome,
  event.reason, event.code, event.severity, event.created, log.level,
  network.protocol, network.transport, tls.version, tls.cipher,
  file.path, file.owner, file.mode, file.uid,
  process.pid, process.name, process.command_line,
  email.from.address, email.to.address, email.message_id,
  rule.id, rule.name, vulnerability.id, vulnerability.severity,
  vulnerability.score.base, observer.vendor, observer.product, service.type
Common fixes you must apply (never output the left form):
  srcip/client_ip -> source.ip ; dstip -> destination.ip ;
  status/status_code -> http.response.status_code ; method -> http.request.method ;
  useragent/ua -> user_agent.original ; referer -> http.request.referrer ;
  username -> user.name ; hostname -> host.name ; cve -> vulnerability.id ;
  proto -> network.protocol ; uri -> url.path
event.outcome must be one of: success, failure, unknown.
If — and only if — ECS has NO suitable field for a value, create a custom field
under a namespace named after the product (e.g. myapp.session_token). Never invent
new sub-fields inside ECS field sets like event.* or source.*.

# AFTER THE YAML
On the final commented line of the YAML, suggest the config.yaml program_mapping
entry, e.g.:  # program_mapping:  myapp_prod: "<pattern_name>"

Here are my RAW LOG SAMPLES:
<<< PASTE 5–20 RAW LOG LINES HERE >>>
````

That's the whole workflow: paste prompt + logs → get YAML → save it in `rules/`
(or paste it into the Web UI's **Rules** editor) → `python3 ecs_helper.py check`
→ add the `program_mapping` line → the running engine hot-reloads the rule
within ~10 seconds, no restart needed. Done.

Where to get the raw samples: the engine's dead-letter queue is your to-do
list — `logs/dlq/<source>.json` holds exactly the lines no pattern matched,
with the raw line embedded. Paste those into the prompt and the gap closes.
