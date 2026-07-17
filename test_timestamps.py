"""
test_timestamps.py — regression test for the two-timestamp model.

Verifies that:
  1. @timestamp holds the event's REAL time parsed from the log (UTC ISO),
     for every supported format, including heavily backdated lines.
  2. event.ingested always holds the parse-time wall clock.
  3. event.timestamp_source is 'log' / 'log_assumed_utc' / 'ingest_fallback'
     correctly — fallbacks are explicit, never silent.
  4. The nessus @timestamp list-corruption bug is gone.

Standalone: imports the real engine + real rules/*.yaml, no Kafka/Redis needed
(the stateful redis path itself needs a live Redis; the non-ID fallback path
of the postfix rule is exercised instead — same _apply_timestamp code).

Run:  python test_timestamps.py     (exit 0 = all pass)
"""
import os
import sys
import json
import time
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import yaml
from core.engine import UniversalEngine
from core.schema import LogInput
from core.timeparse import parse_timestamp

HERE = os.path.dirname(os.path.abspath(__file__))
RESULTS = []


def check(label, produced, expected, extra_ok=True, note=""):
    ok = (produced == expected) and extra_ok
    RESULTS.append((label, produced, expected, ok, note))
    return ok


def load_rule(fname):
    with open(os.path.join(HERE, "rules", fname), "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def envelope(program, raw):
    return json.dumps({"meta": {"source_program": program}, "raw": raw})


def apache_line(ts):
    return ('203.0.113.7 - - [%s] "GET /x HTTP/1.1" 200 12 "-" "curl"' % ts)


# ---------------------------------------------------------------------------
# Part A — parse_timestamp unit cases (format coverage, deterministic clock)
# ---------------------------------------------------------------------------
NOW_2026_07_09 = datetime(2026, 7, 9, 12, 0, 0, tzinfo=timezone.utc).timestamp()
NOW_2026_01_02 = datetime(2026, 1, 2, 0, 0, 0, tzinfo=timezone.utc).timestamp()

UNIT_CASES = [
    # label, (value, fmt, tz), expected (iso, source)
    ("CLF +0530 backdated 2019",
     ("09/Jul/2019:13:31:48 +0530", "clf", None),
     ("2019-07-09T08:01:48+00:00", "log")),
    ("CLF UTC yesterday",
     ("08/Jul/2026:10:00:00 +0000", "clf", None),
     ("2026-07-08T10:00:00+00:00", "log")),
    ("ISO8601 +05:30",
     ("2026-07-01T13:31:48+05:30", "iso8601", None),
     ("2026-07-01T08:01:48+00:00", "log")),
    ("ISO8601 Zulu",
     ("2020-01-01T00:00:00Z", "iso8601", None),
     ("2020-01-01T00:00:00+00:00", "log")),
    ("ISO8601 microseconds",
     ("2026-07-01T13:31:48.123456Z", "iso8601", None),
     ("2026-07-01T13:31:48.123456+00:00", "log")),
    ("ISO8601 nanoseconds (truncated)",
     ("2026-07-01T13:31:48.123456789Z", "iso8601", None),
     ("2026-07-01T13:31:48.123456+00:00", "log")),
    ("RFC5424 (ISO profile)",
     ("2026-07-09T13:31:48.123456+05:30", "iso8601", None),
     ("2026-07-09T08:01:48.123456+00:00", "log")),
    ("ISO8601 no zone, no tz declared",
     ("2026-07-01T13:31:48", "iso8601", None),
     ("2026-07-01T13:31:48+00:00", "log_assumed_utc")),
    ("ISO8601 no zone, tz declared",
     ("2026-07-01T13:31:48", "iso8601", "+05:30"),
     ("2026-07-01T08:01:48+00:00", "log")),
    ("Epoch seconds (2020)",
     ("1594282308", "epoch", None),
     ("2020-07-09T08:11:48+00:00", "log")),
    ("Epoch milliseconds",
     ("1594282308000", "epoch", None),
     ("2020-07-09T08:11:48+00:00", "log")),
    ("Epoch microseconds",
     ("1594282308000000", "epoch", None),
     ("2020-07-09T08:11:48+00:00", "log")),
    ("Epoch as JSON int",
     (1594282308, "epoch", None),
     ("2020-07-09T08:11:48+00:00", "log")),
    ("CLF 12-hour PM",
     ("09/Jul/2026:01:31:48 PM +0530", "clf", None),
     ("2026-07-09T08:01:48+00:00", "log")),
    ("CLF 12-hour 12 AM (midnight)",
     ("09/Jul/2026:12:05:00 AM +0000", "clf", None),
     ("2026-07-09T00:05:00+00:00", "log")),
    ("Nepal +0545",
     ("09/Jul/2026:13:31:48 +0545", "clf", None),
     ("2026-07-09T07:46:48+00:00", "log")),
    ("Ambiguous IST -> refuse",
     ("09/Jul/2026:13:31:48 IST", "clf", None),
     (None, None)),
    ("Ambiguous CET (DST trap) -> refuse",
     ("26/Oct/2025:01:30:00 CET", "clf", None),
     (None, None)),
    ("French month -> refuse (no locale guessing)",
     ("09/juil./2026:13:31:48 +0200", "clf", None),
     (None, None)),
    ("Leap second clamps to :59",
     ("31/Dec/2016:23:59:60 +0000", "clf", None),
     ("2016-12-31T23:59:59.999999+00:00", "log")),
    ("Padding + trailing junk tolerated",
     ("  09/Jul/2026:13:31:48 +0530  ", "clf", None),
     ("2026-07-09T08:01:48+00:00", "log")),
    ("Pure garbage -> refuse",
     ("not a time at all", "clf", None),
     (None, None)),
    ("US-style via explicit strptime",
     ("03/04/2026 10:00:00", "%m/%d/%Y %H:%M:%S", None),
     ("2026-03-04T10:00:00+00:00", "log_assumed_utc")),
    ("Suricata fast.log (2023)",
     ("07/09/2023-13:31:48.123456", "suricata", None),
     ("2023-07-09T13:31:48.123456+00:00", "log_assumed_utc")),
    ("Suricata with declared sensor tz",
     ("07/09/2023-13:31:48.123456", "suricata", "+05:30"),
     ("2023-07-09T08:01:48.123456+00:00", "log")),
    ("nginx error format",
     ("2026/07/01 10:00:00", "nginx_error", None),
     ("2026-07-01T10:00:00+00:00", "log_assumed_utc")),
    ("ModSecurity asctime",
     ("Tue Jul  8 10:15:30 2025", "asctime", None),
     ("2025-07-08T10:15:30+00:00", "log_assumed_utc")),
    ("Roundcube dd-Mon-yyyy +offset",
     ("08-Jul-2025 14:22:10 +0530", "roundcube", None),
     ("2025-07-08T08:52:10+00:00", "log")),
]

RFC3164_CASES = [
    # label, value, tz, now_ts, expected
    ("RFC3164 normal (mid-year)",
     "Jun 16 10:00:00", None, NOW_2026_07_09,
     ("2026-06-16T10:00:00+00:00", "log_assumed_utc")),
    ("RFC3164 with declared tz",
     "Jun 16 10:00:00", "+05:30", NOW_2026_07_09,
     ("2026-06-16T04:30:00+00:00", "log")),
    ("RFC3164 Dec log read on Jan 2 -> PREVIOUS year",
     "Dec 31 23:59:59", None, NOW_2026_01_02,
     ("2025-12-31T23:59:59+00:00", "log_assumed_utc")),
    ("RFC3164 Jan log read on Jan 2 -> current year",
     "Jan  1 00:00:05", None, NOW_2026_01_02,
     ("2026-01-01T00:00:05+00:00", "log_assumed_utc")),
    ("RFC3164 >48h future skew -> previous year",
     "Jul 20 12:00:00", None, NOW_2026_07_09,
     ("2025-07-20T12:00:00+00:00", "log_assumed_utc")),
]


def run_unit_cases():
    for label, (value, fmt, tz), expected in UNIT_CASES:
        got = parse_timestamp(value, fmt, tz, now_ts=NOW_2026_07_09)
        check("[unit] " + label, got, expected)
    for label, value, tz, now_ts, expected in RFC3164_CASES:
        got = parse_timestamp(value, "rfc3164", tz, now_ts=now_ts)
        check("[unit] " + label, got, expected)


# ---------------------------------------------------------------------------
# Part B — end-to-end through the real engine with the real rules
# ---------------------------------------------------------------------------
def run_engine_cases():
    engines = {name: UniversalEngine(load_rule(name + ".yaml"))
               for name in ["apache", "auth", "suricata", "nginx", "postfix",
                            "modsec", "fim", "nessus", "openvas", "roundcube"]}
    wall = time.time()

    def process(key, raw):
        out = engines[key].process(LogInput(envelope(key, raw)))
        return out[0] if isinstance(out, list) else out

    def verify(label, ev, want_ts, want_source):
        if ev is None:
            check("[engine] " + label, "<dropped>", want_ts)
            return
        ts = ev.get("@timestamp")
        src = ev.get("event", {}).get("timestamp_source")
        ing = ev.get("event", {}).get("ingested")
        ing_ok = (isinstance(ing, str)
                  and abs(datetime.fromisoformat(ing).timestamp() - wall) < 5)
        if want_ts == "INGEST":  # fallback: @timestamp must equal ingested
            check("[engine] " + label, (ts == ing, src), (True, want_source),
                  extra_ok=ing_ok, note="@timestamp==event.ingested")
        else:
            check("[engine] " + label, (ts, src), (want_ts, want_source),
                  extra_ok=ing_ok,
                  note="" if ing_ok else "event.ingested BAD: %r" % ing)

    # Apache: THE original bug case — a 2019 line must come out as 2019
    verify("apache CLF backdated to 2019",
           process("apache", apache_line("09/Jul/2019:13:31:48 +0530")),
           "2019-07-09T08:01:48+00:00", "log")
    verify("apache CLF +0545 (Nepal)",
           process("apache", apache_line("09/Jul/2026:13:31:48 +0545")),
           "2026-07-09T07:46:48+00:00", "log")
    verify("apache garbage ts -> explicit fallback",
           process("apache", apache_line("not a time at all")),
           "INGEST", "ingest_fallback")
    verify("apache ambiguous IST -> explicit fallback",
           process("apache", apache_line("09/Jul/2026:13:31:48 IST")),
           "INGEST", "ingest_fallback")

    # Auth: RFC3164, year inferred at real 'now' — compute expectation the
    # same way (Jan 5 is always in the past of a running year)
    year = datetime.now(timezone.utc).year
    verify("auth RFC3164 (no year, no tz)",
           process("auth", "Jan  5 04:00:00 mail sshd[11]: Failed password "
                           "for root from 198.51.100.9 port 22 ssh2"),
           "%d-01-05T04:00:00+00:00" % year, "log_assumed_utc")

    # Suricata: own format, sensor-local, 2023
    verify("suricata backdated 2023",
           process("suricata",
                   "07/09/2023-13:31:48.123456  [**] [1:2001219:20] ET SCAN "
                   "[**] [Classification: Recon] [Priority: 2] {TCP} "
                   "1.2.3.4:44 -> 5.6.7.8:80"),
           "2023-07-09T13:31:48.123456+00:00", "log_assumed_utc")

    # Nginx: access (CLF, rule-level), error (per-pattern override),
    # ssl failure (rsyslog ISO prefix via regex)
    verify("nginx access CLF (backdated)",
           process("nginx",
                   "2026-05-21T10:54:11+00:00 web1 nginx: 1.2.3.4 - - "
                   "[21/May/2026:10:54:11 +0530] \"GET /a HTTP/1.1\" 200 5 "
                   "\"-\" \"curl\""),
           "2026-05-21T05:24:11+00:00", "log")
    verify("nginx error format (per-pattern override)",
           process("nginx",
                   "2026-06-02T08:00:05+00:00 web1 nginx: 2026/06/02 08:00:04 "
                   "[error] 11#0: *7 connect() failed (111: Connection refused) "
                   "while connecting to upstream, client: 1.2.3.4, server: s1, "
                   "request: \"GET /x HTTP/1.1\", upstream: \"http://u\", "
                   "host: \"h\""),
           "2026-06-02T08:00:04+00:00", "log_assumed_utc")
    verify("nginx ssl failure (rsyslog prefix)",
           process("nginx",
                   "2026-06-02T08:00:05+05:30 web1 nginx: 2026/06/02 08:00:05 "
                   "[info] 1#0: *9 peer closed connection in SSL handshake"),
           "2026-06-02T02:30:05+00:00", "log")

    # Postfix: non-ID line -> stateless fallback path, rsyslog ISO prefix
    verify("postfix connect line (rsyslog prefix)",
           process("postfix",
                   "2026-01-22T12:27:51+00:00 smtp1 postfix/smtpd[41]: "
                   "connect from client.example.net[203.0.113.45]"),
           "2026-01-22T12:27:51+00:00", "log")

    # ModSecurity: asctime inside JSON
    verify("modsec asctime (backdated)",
           process("modsec", json.dumps({
               "transaction": {"unique_id": "x1",
                               "time_stamp": "Tue Jul  8 10:15:30 2025",
                               "client_ip": "1.2.3.4"}})),
           "2025-07-08T10:15:30+00:00", "log_assumed_utc")

    # FIM: no time field exists in the JSON -> documented fallback
    verify("fim (no time field, by design)",
           process("fim", '{"action":"modified","file_path":"/etc/passwd",'
                          '"owner":"root","owner_uid":0,"mode":"0644"}'),
           "INGEST", "ingest_fallback")

    # Nessus: the old LIST-corruption case -> clean epoch-parsed string
    ev = process("nessus", json.dumps({
        "info": {"uuid": "u1", "name": "scan", "policy": "p",
                 "scan_start": 1594282308, "scan_end": 1594285908,
                 "status": "completed", "targets": "10.0.0.0/24",
                 "scanner_name": "s1"},
        "vulnerabilities": [], "hosts": []}))
    verify("nessus epoch scan_start (was list bug)",
           ev, "2020-07-09T08:11:48+00:00", "log")
    check("[engine] nessus @timestamp is a plain string (not list)",
          type(ev.get("@timestamp")).__name__, "str")

    # OpenVAS: XML result with Zulu creation_time
    verify("openvas XML creation_time (backdated)",
           process("openvas",
                   "<report><results><result><name>V</name>"
                   "<creation_time>2024-03-05T10:00:00Z</creation_time>"
                   "<host>9.9.9.9</host><port>80</port>"
                   "</result></results></report>"),
           "2024-03-05T10:00:00+00:00", "log")

    # Roundcube: bracketed prefix with offset
    verify("roundcube prefix (backdated)",
           process("roundcube",
                   "[08-Jul-2025 14:22:10 +0530]: <a1b2c3> Failed login for "
                   "admin from 10.0.0.1 (X-Real-IP: 203.0.113.5). ..."),
           "2025-07-08T08:52:10+00:00", "log")


def main():
    run_unit_cases()
    run_engine_cases()

    w = max(len(r[0]) for r in RESULTS)
    fails = 0
    print("%-*s | %-6s | %s" % (w, "CASE", "RESULT", "PRODUCED -> EXPECTED"))
    print("-" * (w + 60))
    for label, produced, expected, ok, note in RESULTS:
        if not ok:
            fails += 1
        detail = "%r" % (produced,) if ok else "%r  !=  %r %s" % (
            produced, expected, note)
        print("%-*s | %-6s | %s" % (w, label, "PASS" if ok else "FAIL",
                                    detail))
    print("-" * (w + 60))
    print("%d/%d passed" % (len(RESULTS) - fails, len(RESULTS)))
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
