# Writing Parsers (Rules) — the ECS-friendly guide

A **rule** is a small YAML file in [`rules/`](rules/) that teaches the engine how
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
regex: '(?P<ip>[\d.]+) "(?P<method>\w+) (?P<path>\S+)" (?P<status>\d+)'
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
    regex: 'Accepted password for (?P<user>\w+) from (?P<ip>[\d.]+)'
    mapping: { user: "user.name", ip: "source.ip" }
    static:  { event.action: "login", event.outcome: "success" }
  - name: "login_fail"
    regex: 'Failed password for (?:invalid user )?(?P<user>\w+) from (?P<ip>[\d.]+)'
    mapping: { user: "user.name", ip: "source.ip" }
    static:  { event.action: "login", event.outcome: "failure" }
```

### `stateful` — stitch multiple lines by a transaction ID
```yaml
pattern_name: "myapp_txn"
strategy: "stateful"
id_regex: '(?P<id>[A-Z0-9]{10,12}):'   # MUST capture a group named 'id'
end_signal: "removed"                   # when this text appears, the event is emitted
patterns:
  - regex: 'client=(?P<host>\S+)\[(?P<ip>[\d.]+)\]'
    mapping: { ip: "source.ip" }
  - regex: 'from=<(?P<sender>[^>]+)>'
    mapping: { sender: "email.from.address" }
```

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
  `status: "http.response.status_code|int"`.
- **Nesting** is automatic from dots: `source.geo.country_name` builds the nested
  object for you.
- **JSON paths** use dots; `*` expands a list: `items.*.id`.
- **XML** uses element paths; `tag/@attr` reads an attribute.
- **`static:`** adds fixed fields to every event (the keys are ECS fields too).
- Repeated mappings to the **same** field automatically become a list.

---

## 5. The most common ECS fields (starter cheat-sheet)

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

**When ECS truly has no field for your value**, keep your own — but put it under a
clear custom namespace named after your product (e.g. `myapp.session_token`), not
inside an ECS set. The helper will mark it `~ custom field, allowed`.

---

## 6. Test your rule

```bash
# Interactive: paste one raw line, see the parsed JSON
python3 test_rules.py

# Whole file: counts parsed vs unparsed, shows samples
python3 test_file.py /path/to/logs.txt myapp_access
python3 test_file.py /path/to/logs.txt AUTO          # let it pick the best rule

# ECS compliance
python3 ecs_helper.py check rules/myapp.yaml
```

## 7. Hook your log source to the rule

In `config.yaml`, map the **source program name** (what your shipper sets) to the
rule's `pattern_name`:
```yaml
program_mapping:
  myapp_prod: "myapp_access"
  myapp_stage: "myapp_access"   # many sources can reuse one rule
```

## 8. Common mistakes

- Using `srcip`, `status`, `useragent`, `cve` → run `ecs_helper.py fix` (they map to
  `source.ip`, `http.response.status_code`, `user_agent.original`, `vulnerability.id`).
- Forgetting `|int` on numbers (they stay strings without it).
- `stateful` rules **must** capture a group named `id` in `id_regex`.
- Putting custom fields inside ECS sets (e.g. `event.my_thing`) — use your own
  namespace instead.

---

## 9. The lazy way — let an AI write the rule for you

Don't want to write regex or learn ECS? Paste the **master prompt** below into any
AI chat (Claude, ChatGPT, Gemini, Grok, …), then paste a few **raw log lines**
underneath. It will hand you a finished, ECS-compliant YAML rule you can drop
straight into `rules/`.

After you paste the AI's YAML into a file, always run:
```bash
python3 ecs_helper.py check rules/your_new_rule.yaml
```
to confirm it's clean before deploying.

### 📋 Master prompt (copy everything in the box)

````text
You are an expert log-parsing engineer for the "FOSS SOC Engine". Your job: read
the RAW LOG SAMPLES I paste at the end and output ONE ready-to-use YAML parser
rule for this engine. Output ONLY the YAML inside a single code block — no
explanation before or after.

# OUTPUT FORMAT
Produce a YAML file with these keys:
- pattern_name: a short unique snake_case name for this log source (string)
- strategy: one of stateless | multi_match | stateful | json_map | xml_xpath
- the strategy-specific keys (below)
- mapping: maps each captured value to an ECS field (see ECS RULES)
- static: (optional) fixed ECS fields added to every event

Choose the strategy by the shape of the logs:
- stateless  : single-line, one consistent format. Keys: regex, mapping, static.
- multi_match: one source with several line formats. Keys: patterns: a list of
               {name, regex, mapping, static}. Order matters; first match wins.
- stateful   : one event spread over multiple lines sharing a transaction id.
               Keys: id_regex (MUST contain a named group `id`), end_signal
               (a substring that marks the final line), patterns: list of
               {regex, mapping, static}.
- json_map   : logs are JSON. mapping keys are dot-paths into the JSON; use `*`
               to walk every element of a list (e.g. items.*.id). Keys: mapping, static.
- xml_xpath  : logs are XML. Keys: items_xpath (element repeated per event),
               mapping where keys are element paths or `tag/@attr` for attributes.

# REGEX RULES
- Use Python `re` syntax with NAMED groups: (?P<name>...).
- Make patterns specific; escape literals; prefer [^"]* / \S+ over greedy .*.
- The group name (left side of mapping) is arbitrary; the ECS field (right side)
  must be valid ECS.

# ECS RULES (critical)
Every value on the RIGHT side of mapping, and every key under static, MUST be a
valid Elastic Common Schema (ECS) field. Add `|int` or `|float` to the ECS field
to coerce numbers, e.g. "http.response.status_code|int".
Use these common ECS fields where they fit:
  source.ip, source.port, source.domain, destination.ip, destination.port,
  host.name, user.name, user_agent.original,
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

That's the whole workflow: paste prompt + logs → get YAML → save it in `rules/` →
`python3 ecs_helper.py check` → add the `program_mapping` line → restart. Done.
