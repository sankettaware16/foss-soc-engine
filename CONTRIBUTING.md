# Contributing to TLSOC Engine

Thank you for helping improve the TLSOC Engine. Parsers, bug fixes,
documentation, and features are all welcome. Ecosystem-wide guidelines live in
[tlsoc — CONTRIBUTING](https://github.com/sankettaware16/tlsoc/blob/main/CONTRIBUTING.md);
this file covers what is specific to the engine.

- [Ground rules](#ground-rules)
- [How a contribution flows](#how-a-contribution-flows)
- [Set up once](#set-up-once)
- [Add a new parser](#add-a-new-parser)
- [Update an existing parser](#update-an-existing-parser)
- [When a check fails](#when-a-check-fails)
- [Contributing code](#contributing-code)
- [Changing the CI/CD pipeline](#changing-the-cicd-pipeline)
- [For maintainers: review, merge, release](#for-maintainers-review-merge-release)
- [Documentation contributions](#documentation-contributions)

## Ground rules

- Be respectful — participation is governed by the
  [Code of Conduct](CODE_OF_CONDUCT.md).
- **Never include real log data** in issues, commits, rules, or golden samples:
  no real IP addresses, hostnames, organization names, usernames, e-mail
  addresses or device serials. CI checks the samples for real-looking IPs, MACs
  and e-mail addresses, but it cannot see a real hostname or name — that part is
  on you. See [Sanitizing samples](#2-collect-and-sanitize-sample-lines).
- Security vulnerabilities go through [SECURITY.md](SECURITY.md), never public
  issues.
- Contributions are accepted under the [Apache-2.0 license](LICENSE).

## How a contribution flows

```
fork + branch ──> change a rule / code ──> python3 test_all.py ──> pull request
                                                                      │
   sites: git pull  <── release tag <── merge to main <── review <── CI (same checks)
   (rules hot-reload)
```

1. You fork the repository and work on a branch.
2. `python3 test_all.py` runs every check locally in a few seconds — the same
   checks CI runs, so green locally means green on GitHub.
3. You open a pull request. CI runs on Python 3.10, 3.12 and 3.13, writes a
   table of results on the run page, and annotates each failure with its fix.
   (First-time contributors: a maintainer approves the first CI run; that is a
   GitHub safety setting, not a problem with your PR.)
4. A maintainer reviews it — mostly the golden-sample diff and the sanitizing.
5. After the merge the parser is on `main`; it ships in the next
   [release](#releases). Sites get it with `git pull`
   ([docs/upgrading.md](docs/upgrading.md)); the running engine loads changed
   rules within about 10 seconds, no restart.

## Set up once

```bash
# fork on GitHub first, then:
git clone https://github.com/<you>/foss-soc-engine.git
cd foss-soc-engine
git remote add upstream https://github.com/sankettaware16/foss-soc-engine.git
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt -r webui/requirements-ui.txt
python3 test_all.py          # all checks should pass before you change anything
```

No Kafka, Redis or Elasticsearch is needed for any of the checks or the
rule-testing tools. Start every new piece of work from a fresh branch:

```bash
git fetch upstream && git checkout -b rule/myapp upstream/main
```

## Add a new parser

A parser is one YAML file in [`rules/`](rules/) plus its golden sample in
[`tests/samples/`](tests/samples/). You do not edit any Python. The full rule
reference is [docs/writing-rules.md](docs/writing-rules.md); this is the path
through it.

### 1. Pick the names

| Name | Example | Used for |
|---|---|---|
| file | `rules/myapp.yaml` | the rule |
| `pattern_name` | `myapp_access` | the rule's ID: golden sample folder, `test_golden.py <name>`, DLQ, Web UI |
| `source` | `myapp` | the log source: Kafka topic `tlsoc.logs.myapp`, index `fosstlsoc-logs-myapp-*` |

`source` is lowercase letters, digits, `_` or `-`, and must not be claimed by
another rule (`grep -h '^source:' rules/*.yaml` lists the taken ones).

### 2. Collect and sanitize sample lines

Pick 5–20 lines that cover **every log type** the parser handles, plus one line
it must **not** match. If the engine already receives this source, the lines no
rule matched are in `logs/dlq/<source>.json` on that server.

Replace every real value, keeping the line layout exactly as it is:

| Real value | Replace with |
|---|---|
| public IPv4 | `192.0.2.x`, `198.51.100.x`, `203.0.113.x` (documentation ranges) |
| internal IPv4 | `10.0.0.x` / `192.168.1.x` is fine, but not your real plan |
| IPv6 | `2001:db8::x` |
| MAC address | `00:00:5e:00:53:xx` |
| user, host, domain | `alice`, `web1`, `example.com` / `example.edu` |
| e-mail | `alice@example.com` |
| serial numbers, UUIDs, session IDs | zeros or `EXAMPLE0001` keeping the length |
| organization / site names | `example_org`, `EXAMPLE` |

`test_samples.py` fails on public IPs, MACs and e-mail addresses that are not
placeholders. If a value really is harmless (a public DNS resolver, the vendor's
documented example address, a version string that looks like an IP), list it in
`tests/samples/<pattern_name>/allow.txt`, one per line, with a `#` comment
saying why.

### 3. Write the rule

```yaml
# rules/myapp.yaml
pattern_name: "myapp_access"
source: "myapp"
strategy: "multi_match"      # stateless | multi_match | stateful | json_map | xml_xpath
...
```

- Choose the strategy with [writing-rules.md §3](docs/writing-rules.md#3-choosing-a-strategy).
- Add a `timestamp:` block ([§5](docs/writing-rules.md#5-event-time--the-timestamp-block-add-it-to-every-rule)) —
  without it every event is stamped with the parse time.
- Map values to ECS fields; put fields ECS has no name for under your own
  namespace (`myapp.*`).
- Shortcuts: the AI master prompt in [§10](docs/writing-rules.md#10-the-lazy-way--let-an-ai-write-the-rule-for-you)
  drafts a rule from sanitized lines, and the Web UI (`python3 webui/app.py`)
  has a rule editor with a live tester.

### 4. Try it on your lines

```bash
python3 test_file.py tests/samples/myapp_access/input.log myapp_access   # parsed vs unparsed
python3 test_rules.py                                                    # paste one line, see the JSON
python3 ecs_helper.py check rules/myapp.yaml                             # fix every 'x'; '~' is fine
```

### 5. Add the golden sample

```bash
mkdir -p tests/samples/myapp_access
cp my-sanitized-lines.log tests/samples/myapp_access/input.log
python3 test_golden.py --update myapp_access     # writes expected.ndjson
```

**Read `expected.ndjson` line by line.** It is the answer key every later change
is compared against: wrong values here become "correct" forever. The first line
is a summary — `no_match` should be the number of lines you put in on purpose
to not match.

### 6. Regenerate the Elasticsearch template

```bash
python3 elasticsearch/generate_template.py
```

New fields get their types (IP, number, keyword) from your rule. Without this,
Elasticsearch guesses the type from the first event, and an IP can end up as
text for good.

### 7. Run every check, then open the PR

```bash
python3 test_all.py
```

Add a line to `CHANGELOG.md` under `[Unreleased]` → `### Added`, commit
(`rules: add MyApp access-log parser`), push to your fork and open the pull
request. In the PR, say where the parser was tested (live traffic, how long,
which log types were seen on real data and which were not).

Commit the four files together:

```
rules/myapp.yaml
tests/samples/myapp_access/input.log
tests/samples/myapp_access/expected.ndjson
elasticsearch/soc-index-template.json
```

## Update an existing parser

For lines a parser misses, a wrong field, or a new log type:

1. **Add the new lines** (sanitized) to the end of the rule's
   `tests/samples/<pattern_name>/input.log`. Do not delete lines — each one
   protects a case someone fixed before.
2. Change the rule, then run `python3 test_golden.py <pattern_name>`. The diff
   shows exactly which answers moved:
   - only your new lines changed → good;
   - old lines changed too → either intended (say so in the PR) or a regression
     to fix.
3. Accept the new answers and review them:
   ```bash
   python3 test_golden.py --update <pattern_name>
   git diff tests/samples/<pattern_name>/expected.ndjson
   ```
4. `python3 elasticsearch/generate_template.py`, then `python3 test_all.py`.
5. `CHANGELOG.md` → `### Fixed` or `### Changed`, then open the PR.

Changes that break other people's dashboards, reports or alerts need a
`### Changed` entry that says so, and a note in the PR:

- **renaming or removing a field** — saved searches and the reporting framework
  query fields by name;
- **changing `source:`** — the topic and the index name change with it;
- **renaming `pattern_name`** — move `tests/samples/<old>/` to the new name too
  (`test_samples.py` fails on a sample folder without a rule).

Site-specific tags never go into a shared rule: they belong in that site's
`config.yaml` `program_mapping` ([writing-rules.md §8](docs/writing-rules.md#8-hook-your-log-source-to-the-rule)).

## When a check fails

`python3 test_all.py` prints, for every failed check, the command to rerun it
alone and the usual fix. CI shows the same on the run page.

| Check | Fails when | Fix |
|---|---|---|
| `config` | a rule does not load, has a non-ECS field name, a ReDoS-prone regex, or a `source` another rule owns | read the `[ERROR]` lines; `python3 ecs_helper.py fix rules/<file>.yaml` corrects common field names |
| `samples` | a rule has no golden sample, a sample folder has no rule, or a sample holds real-looking data | add the sample; replace the value, or list a harmless one in `allow.txt` |
| `golden` | a rule's output for its sample changed | intended: `test_golden.py --update <name>` and review the diff; not intended: fix the rule |
| `template` | rules changed but `soc-index-template.json` did not | `python3 elasticsearch/generate_template.py`, commit the JSON |
| `timestamps` | a `timestamp:` block does not parse its format | [writing-rules.md §5](docs/writing-rules.md#5-event-time--the-timestamp-block-add-it-to-every-rule) |
| `sources` | names, aliases or fallback chains route wrongly | [writing-rules.md §8](docs/writing-rules.md#8-hook-your-log-source-to-the-rule) |
| `skew`, `enrichment`, `internal_map` | engine behaviour changed | these only fail on engine-code changes; run the script alone to see which case |
| `webui` | the Web UI does not start, log in, list rules or test a rule | `python3 webui/app.py`, open the page that failed |
| `lint` (CI only) | a Python syntax error or undefined name | `pip install ruff && ruff check --select E9,F63,F7,F82 .` |

## Contributing code

1. **Open an issue first** for anything beyond a small fix, so the approach can
   be agreed before you invest time.
2. Branch from `main`; keep the diff focused on one topic.
3. Match the existing code style of the module you touch; avoid drive-by
   refactoring.
4. Engine behaviour gets a regression case in the matching `test_*.py`; a new
   area gets its own test file (see the next section).
5. Run `python3 test_all.py`. For Web UI changes, also click through the
   affected screens (`python3 webui/app.py`).
6. Open a PR with the template; update `CHANGELOG.md` under `[Unreleased]` for
   user-visible changes.

## Changing the CI/CD pipeline

| File | What it does |
|---|---|
| [`test_all.py`](test_all.py) | the list of checks; CI and contributors both run it |
| [`.github/workflows/ci.yml`](.github/workflows/ci.yml) | runs `test_all.py` on Python 3.10 / 3.12 / 3.13 on every push and PR, plus the lint job |
| [`.github/workflows/release.yml`](.github/workflows/release.yml) | on a `v*.*.*` tag: re-runs the checks, publishes the GitHub release |
| [`.github/dependabot.yml`](.github/dependabot.yml) | monthly grouped PRs for GitHub Actions and Python packages |
| [`.github/CODEOWNERS`](.github/CODEOWNERS) | requests the maintainer's review on every PR |

**Add a check:** write `test_<name>.py` that needs no Kafka/Redis, prints what
it tests and exits non-zero on failure; add one entry to `CHECKS` in
`test_all.py` (key, command, what it proves, how to fix it) and one row to the
table in [docs/development.md](docs/development.md#the-regression-battery-ci).
The workflow needs no change.

**Change the Python versions:** edit the matrix in `ci.yml` and the versions
named in this file and in docs/development.md.

**Change a workflow:** keep `permissions: contents: read` on CI (fork PRs must
not get a write token) and never use `pull_request_target` to run PR code.
Test a workflow change on a branch in your fork first — Actions run there too.

## For maintainers: review, merge, release

### Reviewing a parser PR

- **First-time contributors**: their CI run waits with "approve and run"
  (Settings → Actions → "Require approval for first-time contributors"). Read
  the diff for anything besides rules, samples and docs before approving.
- Read `tests/samples/<name>/input.log`: sanitized? Hostnames, org names and
  user names are not checked by CI.
- Read `expected.ndjson`: right fields, right ECS names, sensible values,
  `no_match` as intended.
- For a changed parser: which existing answers moved, and is that intended?
- Speed, for a high-volume source: `python3 benchmark.py --rule <pattern_name>`.
- CI must be green. Prefer **squash merge** with a clear title
  (`rules: add FortiGate syslog parser (#9)`).

If a contributor's PR is close, it is often faster to push a small fix to their
branch (enabled by default on fork PRs) than to ask for another round.

### Releases

1. In `CHANGELOG.md`, rename `## [Unreleased]` to `## [X.Y.Z] - YYYY-MM-DD`
   and add a new empty `## [Unreleased]` above it. Commit to `main`.
   Semantic Versioning: a new parser or option is a minor version, fixes a
   patch version, a renamed field or removed option a major version.
2. Tag and push:
   ```bash
   git tag -a vX.Y.Z -m "vX.Y.Z" && git push origin vX.Y.Z
   ```
3. The [release workflow](.github/workflows/release.yml) re-runs all checks and
   publishes the release with the changelog section, the source archive, a
   rules-only archive, the index template and `SHA256SUMS`. A tag like
   `vX.Y.Z-rc1` becomes a pre-release.

Nothing is deployed automatically — each site upgrades on its own schedule with
[docs/upgrading.md](docs/upgrading.md).

## Documentation contributions

READMEs and `docs/` follow the ecosystem
[standards](https://github.com/sankettaware16/tlsoc/blob/main/docs/ecosystem.md)
and [branding](https://github.com/sankettaware16/tlsoc/blob/main/docs/branding.md).
Long-form content belongs in `docs/`; the README stays an overview.
