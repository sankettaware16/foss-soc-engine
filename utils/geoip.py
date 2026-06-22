import ipaddress
import os
import functools
import yaml

# geoip2 is optional. When it is not installed (e.g. a lightweight web-UI /
# local-testing install), GeoIP enrichment is simply skipped instead of
# crashing the import. Parsing works exactly the same, just without geo fields.
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

    def __new__(cls, config_path="config.yaml"):
        if cls._instance is None:
            cls._instance = super(GeoIPClient, cls).__new__(cls)
            cls._instance._initialize(config_path)
        return cls._instance

    def _initialize(self, config_file):
        if geoip2 is None:
            print("GeoIP library (geoip2) not installed - geo enrichment disabled")
            self._reader = None
            self._lookup = functools.lru_cache(maxsize=GEOIP_CACHE_SIZE)(
                self._lookup_uncached
            )
            return
        try:
            # Resolve config path relative to the project root
            base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            config_path = os.path.join(base_dir, config_file)

            with open(config_path, 'r') as f:
                conf = yaml.safe_load(f)

            db_rel_path = conf.get('geoip', {}).get('db_path')

            if db_rel_path:
                db_abs_path = os.path.join(base_dir, db_rel_path)
                if os.path.exists(db_abs_path):
                    self._reader = geoip2.database.Reader(db_abs_path)
                    print(f"GeoIP Database loaded: {db_abs_path}")
                else:
                    print(f"GeoIP Database file not found: {db_abs_path}")
                    self._reader = None
            else:
                self._reader = None

        except Exception as e:
            print(f"GeoIP initialization error: {e}")
            self._reader = None

        # Per-process memoized lookup. Bound to this instance so each worker
        # process keeps its own cache (fork-safe, no shared state).
        self._lookup = functools.lru_cache(maxsize=GEOIP_CACHE_SIZE)(
            self._lookup_uncached
        )

    def _lookup_uncached(self, ip_str):
        try:
            ip_obj = ipaddress.ip_address(ip_str)
            if ip_obj.is_private or ip_obj.is_loopback:
                return None

            response = self._reader.city(ip_str)

            return {
                "country_name": response.country.name,
                "country_iso_code": response.country.iso_code,
                "city_name": response.city.name,
                "location": {
                    "lat": response.location.latitude,
                    "lon": response.location.longitude
                }
            }
        except Exception:
            return None

    def enrich(self, ip_str):
        if not self._reader or not ip_str:
            return None
        return self._lookup(ip_str)
