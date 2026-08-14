#!/usr/bin/env python3
"""
ECS rule helper - an "autocorrect" for the field names in your parser rules.

Not everyone knows the Elastic Common Schema (ECS) by heart, so this tool tells
you, while you write a rule, whether each mapping target is a real ECS field and
- if not - what you most likely meant.

Usage:
  python3 ecs_helper.py check rules/postfix.yaml      # check one rule (or a dir)
  python3 ecs_helper.py check rules/                   # check every rule
  python3 ecs_helper.py fix rules/postfix.yaml         # auto-apply safe corrections
  python3 ecs_helper.py find "country"                 # search ECS fields by concept
  python3 ecs_helper.py                                # interactive lookup

Exit code is non-zero from `check` when a rule contains a field that has a known
ECS correction (so it can gate a deploy). Genuine custom fields are allowed.
"""

import os
import re
import sys
import argparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import yaml
from core import ecs_schema as E

GREEN, YELLOW, RED, DIM, BOLD, RST = (
    "\033[32m", "\033[33m", "\033[31m", "\033[2m", "\033[1m", "\033[0m"
)
if not sys.stdout.isatty():
    GREEN = YELLOW = RED = DIM = BOLD = RST = ""


def iter_targets(rule):
    """Yield (target_field, location_label) for every ECS target in a rule."""
    def from_block(mapping, static, where):
        if isinstance(mapping, dict):
            for src, tgt in mapping.items():
                # a list value fans one capture out to several fields
                for t in (tgt if isinstance(tgt, list) else [tgt]):
                    yield t, f"{where} mapping[{src}]"
        if isinstance(static, dict):
            for k in static.keys():
                yield k, f"{where} static"

    yield from from_block(rule.get("mapping"), rule.get("static"), "rule")
    for i, p in enumerate(rule.get("patterns") or [], 1):
        if isinstance(p, dict):
            label = p.get("name") or f"pattern#{i}"
            yield from from_block(p.get("mapping"), p.get("static"), label)


def load_rule(path):
    with open(path) as f:
        return yaml.safe_load(f)


def rule_files(target):
    if os.path.isdir(target):
        return [os.path.join(target, f) for f in sorted(os.listdir(target))
                if f.endswith(".yaml")]
    return [target]


def cmd_check(target):
    files = rule_files(target)
    total_err = 0
    total_custom = 0
    for path in files:
        try:
            rule = load_rule(path)
        except Exception as e:
            print(f"{RED}ERROR{RST} {path}: cannot parse YAML: {e}")
            total_err += 1
            continue
        if not isinstance(rule, dict):
            continue

        name = rule.get("pattern_name", os.path.basename(path))
        problems, customs = [], []
        for field, loc in iter_targets(rule):
            if not isinstance(field, str):
                continue
            status, sug = E.classify(field)
            if status in ("alias", "typo"):
                problems.append((field, sug, loc))
            elif status == "custom":
                customs.append((field, sug, loc))

        header = f"{BOLD}{name}{RST}  {DIM}({os.path.basename(path)}){RST}"
        if not problems and not customs:
            print(f"{GREEN}OK{RST}    {header} - all fields are valid ECS")
        else:
            flag = f"{RED}FIX{RST} " if problems else f"{YELLOW}NOTE{RST}"
            print(f"{flag}  {header}")
            for field, sug, loc in problems:
                print(f"      {RED}x{RST} {field}  ->  use {GREEN}{sug}{RST}   {DIM}{loc}{RST}")
            for field, sug, loc in customs:
                hint = f"   {DIM}(closest ECS: {sug}){RST}" if sug else ""
                print(f"      {YELLOW}~{RST} {field}  {DIM}custom field, allowed{RST}{hint}   {DIM}{loc}{RST}")
        total_err += len(problems)
        total_custom += len(customs)

    print()
    if total_err:
        print(f"{RED}{total_err} field(s) need fixing{RST}, {total_custom} custom field(s) allowed.")
        print(f"Run {BOLD}python3 ecs_helper.py fix {target}{RST} to apply the safe corrections.")
        return 1
    print(f"{GREEN}All fields valid ECS.{RST} {total_custom} custom field(s) allowed.")
    return 0


def cmd_fix(target):
    files = rule_files(target)
    changed = 0
    for path in files:
        with open(path) as f:
            text = f.read()
        try:
            rule = yaml.safe_load(text)
        except Exception:
            continue
        if not isinstance(rule, dict):
            continue

        edits = {}  # wrong -> right (deduped)
        for field, _loc in iter_targets(rule):
            if not isinstance(field, str):
                continue
            status, sug = E.classify(field)
            if status in ("alias", "typo") and sug:
                base = E.strip_type(field)
                suffix = field[len(base):]      # preserve |int / |float
                edits[field] = sug + suffix

        if not edits:
            continue

        new_text = text
        for wrong, right in edits.items():
            # Replace the field only where it appears as a quoted YAML value/key,
            # so we never touch a substring of a longer field.
            for q in ('"', "'"):
                new_text = new_text.replace(f"{q}{wrong}{q}", f"{q}{right}{q}")
            # bare (unquoted) static key:  "  wrong: ..."
            new_text = re.sub(rf"(^\s*){re.escape(wrong)}(\s*:)",
                              rf"\g<1>{right}\g<2>", new_text, flags=re.M)

        if new_text != text:
            with open(path, "w") as f:
                f.write(new_text)
            changed += 1
            print(f"{GREEN}fixed{RST} {os.path.basename(path)}:")
            for wrong, right in edits.items():
                print(f"    {wrong}  ->  {right}")

    print(f"\n{changed} file(s) updated." if changed else "Nothing to fix.")
    return 0


def cmd_find(words):
    concept = " ".join(words)
    hits = E.search(concept, n=15)
    if not hits:
        print(f"No ECS field matches '{concept}'. Try a simpler word (ip, user, http, mail).")
        return 1
    print(f"ECS fields for {BOLD}{concept}{RST}:")
    for f in hits:
        print(f"  {GREEN}{f}{RST}")
    return 0


def interactive():
    print(f"{BOLD}ECS field helper{RST} - type a field name or a concept; 'exit' to quit.")
    print(f"{DIM}examples:  srcip   |   http status   |   email.from   |   country{RST}\n")
    while True:
        try:
            q = input("ecs> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not q or q.lower() in ("exit", "quit"):
            break
        status, sug = E.classify(q)
        if status == "ecs":
            print(f"  {GREEN}valid ECS field{RST}")
        elif status in ("alias", "typo"):
            print(f"  {RED}not ECS{RST} - use  {GREEN}{sug}{RST}")
        else:
            hits = E.search(q, 8)
            if sug:
                print(f"  {YELLOW}custom field (allowed){RST}; closest ECS: {GREEN}{sug}{RST}")
            if hits:
                print("  ECS fields that match:")
                for f in hits:
                    print(f"    {GREEN}{f}{RST}")
            elif not sug:
                print(f"  {YELLOW}no ECS match{RST} - if ECS has no field for this, keep it as a custom field.")


def main():
    ap = argparse.ArgumentParser(description="ECS autocorrect helper for parser rules.")
    sub = ap.add_subparsers(dest="cmd")
    c = sub.add_parser("check", help="check rule file(s) for ECS compliance")
    c.add_argument("path")
    f = sub.add_parser("fix", help="auto-apply safe ECS corrections to rule file(s)")
    f.add_argument("path")
    fi = sub.add_parser("find", help="search ECS fields by concept")
    fi.add_argument("words", nargs="+")
    args = ap.parse_args()

    if args.cmd == "check":
        sys.exit(cmd_check(args.path))
    elif args.cmd == "fix":
        sys.exit(cmd_fix(args.path))
    elif args.cmd == "find":
        sys.exit(cmd_find(args.words))
    else:
        interactive()


if __name__ == "__main__":
    main()
