"""
test_enrichment.py — regression test for GeoIP + ASN enrichment.

Verifies that:
  1. Events whose source.ip is public get source.geo.* (City DB) and
     source.as.number / source.as.organization.name (ASN DB).
  2. Private/loopback IPs are never enriched.
  3. geoip.enabled: false in config disables BOTH lookups.
  4. A missing database or a missing geoip2 library degrades gracefully
     (fields skipped, engine keeps parsing).

Part A (plumbing) always runs — it injects fake mmdb readers, no database or
geoip2 needed. Part B (real lookups) runs only when geoip2 is installed AND
database/GeoLite2-City.mmdb + database/GeoLite2-ASN.mmdb exist; MaxMind's
official test databases work fine for this:
  https://github.com/maxmind/MaxMind-DB/tree/main/test-data
  (GeoIP2-City-Test.mmdb / GeoLite2-ASN-Test.mmdb, renamed to the prod names)

Run:  python test_enrichment.py     (exit 0 = all pass)
"""
import os
import sys
import json

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import yaml
from utils.geoip import GeoIPClient
from core.engine import UniversalEngine
from core.schema import LogInput

RESULTS = []


def check(label, ok, note=""):
    RESULTS.append((label, bool(ok), note))


def reset_singleton():
    GeoIPClient._instance = None
    GeoIPClient._reader = None
    GeoIPClient._asn_reader = None


def apache_event(ip):
    with open(os.path.join(HERE, "rules", "apache.yaml"), encoding="utf-8") as f:
        rule = yaml.safe_load(f)
    eng = UniversalEngine(rule)
    raw = ('%s - - [09/Jul/2026:10:00:00 +0000] "GET /x HTTP/1.1" 200 1 '
           '"-" "curl"' % ip)
    env = json.dumps({"meta": {"source_program": "apache"}, "raw": raw})
    return eng.process(LogInput(env))


# --------------------------------------------------------------------------
# Part A — plumbing with fake readers (always runs)
# --------------------------------------------------------------------------
class _FakeCityResp:
    class country:
        name = "Testland"
        iso_code = "TL"
    class city:
        name = "Faketown"
    class location:
        latitude = 1.5
        longitude = 2.5


class _FakeAsnResp:
    autonomous_system_number = 64512
    autonomous_system_organization = "Fake Networks Ltd"


class _FakeCityReader:
    def city(self, ip):
        return _FakeCityResp()


class _FakeAsnReader:
    def asn(self, ip):
        return _FakeAsnResp()


def part_a():
    reset_singleton()
    client = GeoIPClient()          # primes the singleton (real init)
    client._reader = _FakeCityReader()
    client._asn_reader = _FakeAsnReader()
    # fresh caches so earlier None results don't stick
    client._lookup.cache_clear()
    client._asn_lookup.cache_clear()

    # NB: must be a genuinely public IP — doc ranges like 203.0.113.0/24
    # count as private for ipaddress and are (correctly) never enriched.
    ev = apache_event("8.8.8.8")
    geo = ev.get("source", {}).get("geo")
    asn = ev.get("source", {}).get("as")
    check("[plumb] public IP gets source.geo",
          geo and geo.get("country_name") == "Testland", repr(geo))
    check("[plumb] public IP gets source.as.number == 64512",
          asn and asn.get("number") == 64512, repr(asn))
    check("[plumb] source.as.organization.name",
          asn and asn.get("organization", {}).get("name") == "Fake Networks Ltd",
          repr(asn))

    ev = apache_event("10.0.0.9")
    check("[plumb] private IP: no geo, no as",
          "geo" not in ev.get("source", {}) and "as" not in ev.get("source", {}),
          repr(ev.get("source")))

    # readers absent -> enrich helpers return None, engine still parses
    client._reader = None
    client._asn_reader = None
    ev = apache_event("8.8.8.8")
    check("[plumb] no readers: event still parsed, no geo/as",
          ev is not None and "as" not in ev.get("source", {})
          and "geo" not in ev.get("source", {}), repr(ev.get("source")))

    check("[plumb] enrich_asn(None reader) is None",
          GeoIPClient().enrich_asn("8.8.8.8") is None)


# --------------------------------------------------------------------------
# Part B — real mmdb lookups (runs when geoip2 + database files exist)
# --------------------------------------------------------------------------
def part_b():
    try:
        import geoip2.database  # noqa: F401
    except Exception:
        check("[real] SKIPPED (geoip2 not installed)", True, "skip")
        return
    city_db = os.path.join(HERE, "database", "GeoLite2-City.mmdb")
    asn_db = os.path.join(HERE, "database", "GeoLite2-ASN.mmdb")
    if not (os.path.exists(city_db) and os.path.exists(asn_db)):
        check("[real] SKIPPED (database/*.mmdb not present)", True, "skip")
        return

    reset_singleton()
    client = GeoIPClient()
    check("[real] City reader loaded", client._reader is not None)
    check("[real] ASN reader loaded", client._asn_reader is not None)

    # 81.2.69.142 = London/GB in MaxMind's City test data (and real GeoLite2)
    ev = apache_event("81.2.69.142")
    geo = ev.get("source", {}).get("geo") or {}
    check("[real] 81.2.69.142 -> geo country GB",
          geo.get("country_iso_code") == "GB", repr(geo))

    # 1.128.0.1 = AS1221 "Telstra Pty Ltd" in MaxMind's ASN test data
    ev = apache_event("1.128.0.1")
    asn = ev.get("source", {}).get("as") or {}
    check("[real] 1.128.0.1 -> as.number 1221", asn.get("number") == 1221,
          repr(asn))
    check("[real] 1.128.0.1 -> as.organization.name Telstra",
          "Telstra" in str(asn.get("organization", {}).get("name")), repr(asn))


# --------------------------------------------------------------------------
# Part C — geoip.enabled: false disables both lookups
# --------------------------------------------------------------------------
def part_c():
    try:
        import geoip2.database  # noqa: F401
    except Exception:
        check("[toggle] SKIPPED (geoip2 not installed)", True, "skip")
        return
    tmp_cfg = "config.test-enrich-off.yaml"
    tmp_abs = os.path.join(HERE, tmp_cfg)
    with open(tmp_abs, "w", encoding="utf-8") as f:
        yaml.safe_dump({"geoip": {
            "enabled": False,
            "db_path": "database/GeoLite2-City.mmdb",
            "asn_db_path": "database/GeoLite2-ASN.mmdb",
        }}, f)
    try:
        reset_singleton()
        client = GeoIPClient(tmp_cfg)
        check("[toggle] enabled:false -> no City reader",
              client._reader is None)
        check("[toggle] enabled:false -> no ASN reader",
              client._asn_reader is None)
        check("[toggle] enabled:false -> enrich() is None",
              client.enrich("81.2.69.142") is None)
        check("[toggle] enabled:false -> enrich_asn() is None",
              client.enrich_asn("1.128.0.1") is None)
    finally:
        os.remove(tmp_abs)
        reset_singleton()


def main():
    part_a()
    part_b()
    part_c()

    w = max(len(r[0]) for r in RESULTS)
    fails = 0
    for label, ok, note in RESULTS:
        if not ok:
            fails += 1
        print("%-*s | %s %s" % (w, label, "PASS" if ok else "FAIL",
                                "" if ok or not note else "-> " + note))
    print("-" * (w + 10))
    print("%d/%d passed" % (len(RESULTS) - fails, len(RESULTS)))
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
