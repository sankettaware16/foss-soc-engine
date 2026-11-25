"""
test_all.py — run every check CI runs, with one command.

    python3 test_all.py              # everything (what CI runs)
    python3 test_all.py golden samples template   # only these checks

Each check is a standalone script (exit 0 = pass); this file only runs them in
order, prints a summary and, for every failure, the command that usually fixes
it. CI (.github/workflows/ci.yml) calls this same file, so a green run here
means a green run there.

Adding a check: write test_<name>.py that exits non-zero on failure, add one
line to CHECKS below and one row to the table in docs/development.md.

The Web UI check needs Flask (pip3 install -r webui/requirements-ui.txt); it is
skipped locally when Flask is missing, but never in CI.
"""
import importlib.util
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
IN_CI = os.environ.get("CI") == "true"
IN_GHA = os.environ.get("GITHUB_ACTIONS") == "true"

# (key, command, what it proves, how to fix a failure)
CHECKS = [
    ("config", ["test_config.py", "--skip-kafka"],
     "config + rules load, ECS field names, ReDoS lint, source names",
     "read the [ERROR] lines; for field names run "
     "`python3 ecs_helper.py check rules/<rule>.yaml`"),
    ("samples", ["test_samples.py"],
     "every rule has a golden sample; samples hold no real data",
     "add tests/samples/<rule>/input.log, replace real IPs/MACs/e-mails "
     "with documentation values"),
    ("golden", ["test_golden.py"],
     "every rule's output for its sample is unchanged",
     "intended change? `python3 test_golden.py --update <rule>`, review "
     "the diff of expected.ndjson and commit it"),
    ("template", ["elasticsearch/generate_template.py", "--check"],
     "the Elasticsearch index template matches the rules",
     "`python3 elasticsearch/generate_template.py` and commit the JSON"),
    ("timestamps", ["test_timestamps.py"],
     "every rule's timestamp: block parses and normalizes to UTC",
     "check the rule's timestamp: block (docs/writing-rules.md §5)"),
    ("sources", ["test_sources.py"],
     "source names, aliases, fallback chains and topic routing",
     "check source:/aliases:/fallback: in the rule (docs/writing-rules.md §8)"),
    ("skew", ["test_skew.py"],
     "future-timestamp detection and correction",
     "engine change: see core/timeparse.py and core/engine.py"),
    ("enrichment", ["test_enrichment.py"],
     "GeoIP/ASN enrichment plumbing",
     "engine change: see utils/geoip.py"),
    ("internal_map", ["test_internal_map.py"],
     "internal IP map ranges and enrichment",
     "engine change: see utils/internal_map.py"),
    ("webui", ["test_webui.py"],
     "Web UI login, pages, rule list and rule tester",
     "run `python3 webui/app.py` and open the failing page"),
]


def flask_available():
    return importlib.util.find_spec("flask") is not None


def run(key, cmd):
    if key == "webui" and not flask_available() and not IN_CI:
        return "SKIP", 0.0, "Flask not installed " \
            "(pip3 install -r webui/requirements-ui.txt)"
    start = time.monotonic()
    proc = subprocess.run([sys.executable] + cmd, cwd=HERE,
                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                          text=True)
    took = time.monotonic() - start
    return ("PASS" if proc.returncode == 0 else "FAIL"), took, proc.stdout


def main():
    only = [a for a in sys.argv[1:] if not a.startswith("-")]
    unknown = set(only) - {k for k, *_ in CHECKS}
    if unknown:
        print(f"unknown check(s): {', '.join(sorted(unknown))}; "
              f"choose from: {', '.join(k for k, *_ in CHECKS)}")
        sys.exit(2)

    results = []
    for key, cmd, proves, fix in CHECKS:
        if only and key not in only:
            continue
        if IN_GHA:
            print(f"::group::{key}: python {' '.join(cmd)}", flush=True)
        else:
            print(f"=== {key}: python3 {' '.join(cmd)}", flush=True)
        status, took, output = run(key, cmd)
        print(output.rstrip())
        if IN_GHA:
            print("::endgroup::")
            if status == "FAIL":
                print(f"::error title={key} failed::{proves} - fix: {fix}")
        print(f"--> {status} ({took:.1f}s)\n", flush=True)
        results.append((key, cmd, proves, fix, status, took))

    width = max(len(r[0]) for r in results)
    print("=" * 60)
    for key, _, proves, _, status, took in results:
        print(f"{status:4}  {key:{width}}  {took:5.1f}s  {proves}")
    failed = [r for r in results if r[4] == "FAIL"]
    for key, cmd, _, fix, _, _ in failed:
        print(f"\n{key} failed - rerun: python3 {' '.join(cmd)}\n  fix: {fix}")
    print("=" * 60)
    print("ALL CHECKS PASSED" if not failed
          else f"{len(failed)} CHECK(S) FAILED: "
               f"{', '.join(r[0] for r in failed)}")

    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as f:
            f.write(f"### Checks on Python {sys.version.split()[0]}\n\n"
                    "| | Check | Proves | If it fails |\n|---|---|---|---|\n")
            for key, cmd, proves, fix, status, _ in results:
                icon = {"PASS": "✅", "FAIL": "❌"}.get(status, "➖")
                f.write(f"| {icon} | `{key}` | {proves} | "
                        f"{fix if status == 'FAIL' else ''} |\n")
            f.write("\n")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
