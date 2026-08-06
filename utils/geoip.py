import ipaddress
import os
import sys
import functools
import yaml

# geoip2 is optional. When it is not installed (e.g. a lightweight web-UI /
# local-testing install), GeoIP/ASN enrichment is simply skipped instead of
# crashing the import. Parsing works exactly the same, just without geo/as fields.
# The SAME library reads both the City and the ASN mmdb - no extra dependency.
try:
    import geoip2.database
except Exception:
    geoip2 = None

# Number of distinct IPs to keep memoized per process. The same source IPs
# (crawlers, scanners, attackers) recur constantly in SOC traffic, so this
# turns the expensive parse + mmdb lookup into a dict hit for repeats.
GEOIP_CACHE_SIZE = 50000

class GeoIPClient:
    _instance = None
    _reader = None
    _asn_reader = None

    def __new__(cls, config_path="config.yaml"):
        if cls._instance is None:
            cls._instance = super(GeoIPClient, cls).__new__(cls)
            cls._instance._initialize(config_path)
        return cls._instance

    def _initialize(self, config_file):
        self._reader = None
        self._asn_reader = None
        # Per-process memoized lookups. Bound to this instance so each worker
        # process keeps its own cache (fork-safe, no shared state). Created
        # unconditionally so enrich()/enrich_asn() are always safe to call.
        self._lookup = functools.lru_cache(maxsize=GEOIP_CACHE_SIZE)(
            self._lookup_uncached
        )
        self._asn_lookup = functools.lru_cache(maxsize=GEOIP_CACHE_SIZE)(
            self._asn_lookup_uncached
        )

        if geoip2 is None:
            print("GeoIP library (geoip2) not installed - geo/ASN enrichment disabled")
            return
        try:
            # Resolve config path relative to the project root. In a frozen
            # (PyInstaller) build __file__ lives in the temp extraction dir,
            # so use the exe's folder instead — that's where the editable
            # config.yaml and the database/ folder sit (same split app.py uses).
            if getattr(sys, 'frozen', False):
                base_dir = os.path.dirname(sys.executable)
            else:
                base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            config_path = os.path.join(base_dir, config_file)

            with open(config_path, 'r') as f:
                conf = yaml.safe_load(f)

            gconf = conf.get('geoip') or {}

            # One switch for ALL IP enrichment (geo + ASN). Missing key = on,
            # so existing configs keep working.
            if not gconf.get('enabled', True):
                print("GeoIP/ASN enrichment disabled in config.yaml (geoip.enabled: false)")
                return

            db_rel_path = gconf.get('db_path')
            if db_rel_path:
                db_abs_path = os.path.join(base_dir, db_rel_path)
                if os.path.exists(db_abs_path):
                    self._reader = geoip2.database.Reader(db_abs_path)
                    print(f"GeoIP Database loaded: {db_abs_path}")
                else:
                    print(f"GeoIP Database file not found: {db_abs_path}")

            # ASN database (GeoLite2-ASN.mmdb): same offline one-time download,
            # same reader library. Omit/comment asn_db_path to disable just ASN.
            asn_rel_path = gconf.get('asn_db_path')
            if asn_rel_path:
                asn_abs_path = os.path.join(base_dir, asn_rel_path)
                if os.path.exists(asn_abs_path):
                    self._asn_reader = geoip2.database.Reader(asn_abs_path)
                    print(f"ASN Database loaded: {asn_abs_path}")
                else:
                    print(f"ASN Database file not found: {asn_abs_path}")

        except Exception as e:
            print(f"GeoIP initialization error: {e}")
            self._reader = None
            self._asn_reader = None

    def _lookup_uncached(self, ip_str):
        try:
            # is_global filters EVERYTHING unroutable in one check: RFC1918,
            # loopback, link-local, CGNAT (100.64/10), multicast, reserved.
            # Costs one parse, saves the mmdb lookup for all of them.
            if not ipaddress.ip_address(ip_str).is_global:
                return None

            response = self._reader.city(ip_str)

            # Sparse output: only what MaxMind actually knows. Anycast/CDN
            # blocks (e.g. Cloudflare 172.67/16) have a record with NO
            # location - the old unconditional dict emitted
            # {"country_name": null, "location": {"lat": null, ...}} junk.
            geo = {}
            if response.country.name:
                geo["country_name"] = response.country.name
            if response.country.iso_code:
                geo["country_iso_code"] = response.country.iso_code
            if response.city.name:
                geo["city_name"] = response.city.name
            lat = response.location.latitude
            lon = response.location.longitude
            if lat is not None and lon is not None:
                geo["location"] = {"lat": lat, "lon": lon}
            return geo or None
        except Exception:
            return None

    def _asn_lookup_uncached(self, ip_str):
        try:
            if not ipaddress.ip_address(ip_str).is_global:
                return None

            response = self._asn_reader.asn(ip_str)

            # ECS: source.as.number / source.as.organization.name
            # Same sparse rule as geo: never emit null-valued fields.
            asn = {}
            if response.autonomous_system_number is not None:
                asn["number"] = response.autonomous_system_number
            if response.autonomous_system_organization:
                asn["organization"] = {
                    "name": response.autonomous_system_organization
                }
            return asn or None
        except Exception:
            return None

    def enrich(self, ip_str):
        if not self._reader or not ip_str:
            return None
        return self._lookup(ip_str)

    def enrich_asn(self, ip_str):
        if not self._asn_reader or not ip_str:
            return None
        return self._asn_lookup(ip_str)
