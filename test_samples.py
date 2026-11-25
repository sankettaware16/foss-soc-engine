"""
test_samples.py — every rule has a golden sample, and no sample holds real data.

The golden exam (test_golden.py) only tests the folders that exist, so a rule
without a sample would pass silently. This file closes that gap and guards the
privacy promise of the repository (no real log data, ever):

  1. Coverage: every rules/*.yaml has tests/samples/<pattern_name>/ with a
     non-empty input.log and an expected.ndjson; every sample folder belongs
     to a rule.
  2. Size: input.log stays a small, hand-picked corpus (MAX_LINES lines,
     MAX_BYTES bytes) - not a dump of a real log file.
  3. Hygiene: input.log must not contain
       - public IP addresses     -> use 192.0.2.x, 198.51.100.x, 203.0.113.x
                                    (or 2001:db8::/32), or private ranges
       - real MAC addresses      -> use 00:00:5e:00:53:xx
       - real e-mail addresses   -> use a domain like example.com / example.edu
     A value that really is fine (a public DNS resolver, an SNMP OID that looks
     like an address, a vendor's documentation address) goes into
     tests/samples/<pattern_name>/allow.txt, one value per line, with a '#'
     comment saying why.

No Kafka/Redis needed. Run:  python3 test_samples.py   (exit 0 = pass)
"""
import ipaddress
import os
import re
import sys

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
SAMPLES_DIR = os.path.join(HERE, "tests", "samples")
RULES_DIR = os.path.join(HERE, "rules")

MAX_LINES = 200
MAX_BYTES = 256 * 1024

# Addresses everybody recognises as harmless (public resolvers, the usual
# placeholders); anything else public needs an allow.txt entry so a reviewer
# sees it.
WELL_KNOWN = {"1.1.1.1", "8.8.8.8", "8.8.4.4", "9.9.9.9", "1.2.3.4", "5.6.7.8"}

_IPV4 = re.compile(r"(?<![\w.])(\d{1,3}(?:\.\d{1,3}){3})(?!\.?\d|\w)")
_VERSION = re.compile(r"\.0\.0\.0$")            # Chrome/141.0.0.0, not an IP
# '.' in the lookahead skips the ::ffff: prefix of an IPv4-mapped address -
# its IPv4 part is checked by _IPV4.
_IPV6 = re.compile(r"(?<![\w:])([0-9A-Fa-f]{0,4}(?::[0-9A-Fa-f]{0,4}){2,7})"
                   r"(?![\w:.])")
_MAC = re.compile(r"(?<![\w:-])([0-9A-Fa-f]{2}(?:[:-][0-9A-Fa-f]{2}){5})"
                  r"(?![\w:-])")
_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@([A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+)")
_RESERVED_TLDS = ("example", "test", "invalid", "localhost", "local")


def rule_names():
    names = {}
    for f in sorted(os.listdir(RULES_DIR)):
        if not f.endswith(".yaml"):
            continue
        with open(os.path.join(RULES_DIR, f), encoding="utf-8") as fh:
            rule = yaml.safe_load(fh) or {}
        names[rule.get("pattern_name", f[:-5])] = f
    return names


def load_allow(folder):
    path = os.path.join(folder, "allow.txt")
    allow = set()
    if os.path.isfile(path):
        with open(path, encoding="utf-8") as f:
            for line in f:
                value = line.split("#", 1)[0].strip()
                if value:
                    allow.add(value.lower())
    return allow


def public_ip(text):
    try:
        ip = ipaddress.ip_address(text)
    except ValueError:
        return False
    if ip.version == 6 and ip in ipaddress.ip_network("2001:db8::/32"):
        return False
    return ip.is_global and text not in WELL_KNOWN


def real_mac(text):
    mac = text.lower().replace("-", ":")
    if mac.startswith("00:00:5e:00:53:"):          # RFC 7042 documentation
        return False
    return mac not in ("00:00:00:00:00:00", "ff:ff:ff:ff:ff:ff")


def real_email_domain(domain):
    labels = domain.lower().split(".")
    if labels[-1] in _RESERVED_TLDS:
        return False
    return "example" not in labels                 # example.com, mail.example.edu


def scan_line(line, allow):
    """Yield (kind, value, fix) for every value in one line that looks real."""
    for m in _IPV4.finditer(line):
        v = m.group(1)
        if v not in allow and not _VERSION.search(v) and public_ip(v):
            yield ("public IP", v, "use 192.0.2.x / 198.51.100.x / 203.0.113.x")
    for m in _IPV6.finditer(line):
        v = m.group(1)
        if v.count(":") >= 2 and v.lower() not in allow and public_ip(v):
            yield ("public IPv6", v, "use 2001:db8::/32")
    for m in _MAC.finditer(line):
        v = m.group(1)
        if v.lower() not in allow and real_mac(v):
            yield ("MAC address", v, "use 00:00:5e:00:53:xx")
    for m in _EMAIL.finditer(line):
        v = m.group(0)
        if v.lower() not in allow and m.group(1).lower() not in allow \
                and real_email_domain(m.group(1)):
            yield ("e-mail address", v, "use user@example.com")


def check_sample(name, folder):
    problems = []
    input_path = os.path.join(folder, "input.log")
    if not os.path.isfile(input_path):
        return [f"{name}: tests/samples/{name}/input.log is missing"]
    if not os.path.isfile(os.path.join(folder, "expected.ndjson")):
        problems.append(f"{name}: expected.ndjson is missing - run: "
                        f"python3 test_golden.py --update {name}")

    size = os.path.getsize(input_path)
    with open(input_path, encoding="utf-8") as f:
        lines = f.read().splitlines()
    count = sum(1 for line in lines if line.strip())
    if count == 0:
        problems.append(f"{name}: input.log has no log lines")
    if count > MAX_LINES or size > MAX_BYTES:
        problems.append(f"{name}: input.log has {count} lines / {size} bytes "
                        f"(max {MAX_LINES} / {MAX_BYTES}) - keep a small, "
                        f"hand-picked set of sanitized lines")

    allow = load_allow(folder)
    for n, line in enumerate(lines, 1):
        for kind, value, fix in scan_line(line, allow):
            problems.append(f"{name}: input.log:{n}: {kind} {value!r} looks "
                            f"real - {fix}, or list it in "
                            f"tests/samples/{name}/allow.txt if it is harmless")
    return problems


def main():
    rules = rule_names()
    folders = sorted(d for d in os.listdir(SAMPLES_DIR)
                     if os.path.isdir(os.path.join(SAMPLES_DIR, d))) \
        if os.path.isdir(SAMPLES_DIR) else []

    failed = []
    for name, fname in rules.items():
        if name not in folders:
            print(f"[FAIL] {name}: rules/{fname} has no golden sample - add "
                  f"tests/samples/{name}/input.log, then run: "
                  f"python3 test_golden.py --update {name}")
            failed.append(name)

    for name in folders:
        if name not in rules:
            print(f"[FAIL] tests/samples/{name}/: no rule has "
                  f"pattern_name '{name}' (renamed rule? move the folder)")
            failed.append(name)
            continue
        problems = check_sample(name, os.path.join(SAMPLES_DIR, name))
        if problems:
            for p in problems:
                print(f"[FAIL] {p}")
            failed.append(name)
        else:
            print(f"[PASS] {name}")

    print("-" * 60)
    print(f"{len(rules)} rules, {len(folders)} samples"
          + (f"  FAILED: {', '.join(sorted(set(failed)))}" if failed
             else "  all covered and sanitized"))
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
