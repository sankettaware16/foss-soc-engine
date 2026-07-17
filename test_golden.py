"""
test_golden.py — golden-sample regression tests, one exam per rule.

Every rule ships with its own tiny corpus:

    tests/samples/<pattern_name>/input.log        <- real raw log lines
    tests/samples/<pattern_name>/expected.ndjson  <- exactly what the engine
                                                     must produce for them

Run modes:
    python test_golden.py            # compare every rule against its expected
                                     # file; non-zero exit + diff on mismatch
    python test_golden.py --update   # regenerate expected files (use ONLY for
                                     # intentional rule changes; review the diff!)
    python test_golden.py <rule>     # test just one rule

This is what makes rule contributions safe: if an edit to any rule changes any
answer for any sample line, CI rejects the change with a diff showing exactly
what broke.

Normalization (so results are identical on every machine and every day):
  - @timestamp and event.ingested are removed (timestamp PARSING correctness
    is covered exhaustively by test_timestamps.py; event.timestamp_source is
    kept so a rule losing its timestamp block still fails the exam)
  - source.geo / source.as are removed (they depend on which mmdb files the
    machine has)
Stateful rules work without a Redis server: a deterministic in-memory fake is
injected, so multi-line transactions (postfix) are fully exercised.
"""
import os
import sys
import json
import fnmatch
import difflib

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import yaml
import core.engine as _engine_mod
from core.engine import UniversalEngine
from core.schema import LogInput

SAMPLES_DIR = os.path.join(HERE, "tests", "samples")
RULES_DIR = os.path.join(HERE, "rules")


class FakeRedis:
    """Deterministic stand-in so stateful rules run without a server."""
    def __init__(self):
        self.store = {}
    def get(self, k):
        return self.store.get(k)
    def set(self, k, v, ex=None):
        self.store[k] = v
    def delete(self, k):
        self.store.pop(k, None)
    def scan_iter(self, match=None, count=None):
        for k in list(self.store):
            if match is None or fnmatch.fnmatch(k, match):
                yield k
    def ttl(self, k):
        return 300
    def eval(self, script, numkeys, key, *args):
        if "EXISTS" in script and "SET" in script:  # conditional update
            if key in self.store:
                self.store[key] = args[0]
                return 1
            return 0
        return self.store.pop(key, None)           # GETDEL


def load_rules_by_name():
    rules = {}
    for f in sorted(os.listdir(RULES_DIR)):
        if not f.endswith(".yaml"):
            continue
        with open(os.path.join(RULES_DIR, f), encoding="utf-8") as fh:
            rule = yaml.safe_load(fh)
        name = rule.get("pattern_name", f[:-5])
        rules[name] = rule
    return rules


def normalize(ev):
    ev = json.loads(json.dumps(ev))  # deep copy
    ev.pop("@timestamp", None)
    if isinstance(ev.get("event"), dict):
        ev["event"].pop("ingested", None)
    src = ev.get("source")
    if isinstance(src, dict):
        src.pop("geo", None)
        src.pop("as", None)
    return ev


def run_rule(rule, input_path):
    """Feed every line of input.log through a fresh engine; return the
    normalized output entries (summary first, then events in order)."""
    _engine_mod.r = FakeRedis()  # fresh transaction state per rule
    engine = UniversalEngine(rule)
    events = []
    lines = 0
    no_match = 0
    buffered = 0
    with open(input_path, encoding="utf-8") as f:
        for raw in f:
            raw = raw.rstrip("\n")
            if not raw.strip():
                continue
            lines += 1
            li = LogInput(json.dumps(
                {"meta": {"source_program": rule.get("pattern_name", "x")},
                 "raw": raw}))
            out = engine.process(li)
            if out is None:
                if getattr(engine, "last_buffered", False):
                    buffered += 1
                else:
                    no_match += 1
            elif isinstance(out, list):
                events.extend(normalize(e) for e in out)
            else:
                events.append(normalize(out))
    summary = {"__summary__": {"lines": lines, "events": len(events),
                               "no_match": no_match, "buffered": buffered}}
    return [summary] + events


def to_ndjson(entries):
    return "\n".join(json.dumps(e, sort_keys=True) for e in entries) + "\n"


def main():
    update = "--update" in sys.argv
    only = [a for a in sys.argv[1:] if not a.startswith("-")]

    rules = load_rules_by_name()
    if not os.path.isdir(SAMPLES_DIR):
        print(f"No samples directory ({SAMPLES_DIR}) - nothing to test")
        sys.exit(0)

    tested = passed = 0
    failed = []
    for name in sorted(os.listdir(SAMPLES_DIR)):
        if only and name not in only:
            continue
        d = os.path.join(SAMPLES_DIR, name)
        input_path = os.path.join(d, "input.log")
        expected_path = os.path.join(d, "expected.ndjson")
        if not os.path.isfile(input_path):
            continue
        rule = rules.get(name)
        if rule is None:
            print(f"[SKIP] {name}: no rule with this pattern_name")
            continue

        actual = to_ndjson(run_rule(rule, input_path))
        tested += 1

        if update:
            with open(expected_path, "w", encoding="utf-8", newline="\n") as f:
                f.write(actual)
            print(f"[UPDATED] {name}  ({actual.count(chr(10)) - 1} events)")
            passed += 1
            continue

        if not os.path.isfile(expected_path):
            print(f"[FAIL] {name}: expected.ndjson missing "
                  f"(run: python test_golden.py --update {name})")
            failed.append(name)
            continue

        with open(expected_path, encoding="utf-8") as f:
            expected = f.read()
        if actual == expected:
            print(f"[PASS] {name}")
            passed += 1
        else:
            print(f"[FAIL] {name}: output changed")
            diff = difflib.unified_diff(
                expected.splitlines(), actual.splitlines(),
                fromfile=f"{name}/expected.ndjson",
                tofile=f"{name}/actual", lineterm="")
            shown = 0
            for line in diff:
                print("   " + line)
                shown += 1
                if shown >= 40:
                    print("   ... (diff truncated)")
                    break
            failed.append(name)

    print("-" * 60)
    print(f"{passed}/{tested} rule corpora passed"
          + (f"  FAILED: {', '.join(failed)}" if failed else ""))
    sys.exit(1 if failed or tested == 0 else 0)


if __name__ == "__main__":
    main()
