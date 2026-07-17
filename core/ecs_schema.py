"""
ECS (Elastic Common Schema) knowledge base + suggestion engine.

This is the single source of truth used by BOTH the rule validator
(test_config.py) and the interactive helper (ecs_helper.py). It answers three
questions about a mapping target field:

  * is_ecs(field)   -> is this an official ECS field (incl. reuse like source.geo.*)?
  * classify(field) -> 'ecs' | 'alias' | 'typo' | 'custom'  (+ a suggestion)
  * suggest(field)  -> ranked ECS fields a typo/alias probably meant
  * search(concept) -> ECS fields matching a plain-English word ("country", "status")

Policy (decided with the project owner):
  - A field that IS ECS                          -> OK
  - A common wrong name with a known ECS target  -> ERROR, with the fix shown
  - A likely typo of an ECS field                -> ERROR, with the fix shown
  - A genuine custom field ECS has no place for  -> ALLOWED (kept as-is)

The ECS field list below is curated (not the full ~1600-field schema) but
covers every field set these rules use plus the common ones people reach for.
Reuse (geo/user/as/nat/os nested under entities) is handled by rule, so you do
not need to enumerate source.geo.*, destination.geo.*, host.os.*, etc.
"""

import difflib

# --- Top-level ECS field sets (namespaces). The first segment of any ECS
#     field must be one of these (or a reserved root field below). ----------
ECS_FIELD_SETS = {
    "agent", "as", "client", "cloud", "code_signature", "container",
    "data_stream", "destination", "device", "dll", "dns", "ecs", "email",
    "error", "event", "faas", "file", "geo", "group", "hash", "host", "http",
    "interface", "log", "network", "observer", "orchestrator", "organization",
    "os", "package", "pe", "process", "registry", "related", "rule", "server",
    "service", "source", "threat", "tls", "trace", "transaction", "url",
    "user", "user_agent", "vlan", "volume", "vulnerability", "x509",
}

# Root fields that live at the document top level (no field set).
RESERVED_ROOT = {"@timestamp", "message", "tags", "labels", "ecs.version", "span.id"}

# Entities that REUSE nested field sets (e.g. source.geo.*, destination.user.*).
ENTITY_PREFIXES = {
    "source", "destination", "client", "server", "host", "observer", "related",
}

# Reusable nested field sets: <set> -> allowed leaf paths under it.
REUSABLE = {
    "geo": {
        "city_name", "continent_code", "continent_name", "country_iso_code",
        "country_name", "location", "name", "postal_code", "region_iso_code",
        "region_name", "timezone",
    },
    "as": {"number", "organization.name"},
    "user": {
        "name", "id", "full_name", "email", "domain", "hash", "roles",
        "group.name", "group.id", "group.domain",
    },
    "nat": {"ip", "port"},
    "os": {
        "family", "full", "kernel", "name", "platform", "type", "version",
    },
    "hash": {
        "md5", "sha1", "sha256", "sha384", "sha512", "ssdeep", "tlsh",
    },
}

# --- Curated set of valid ECS leaf fields (flat dotted paths). --------------
ECS_FIELDS = {
    # event
    "event.action", "event.category", "event.code", "event.created",
    "event.dataset", "event.duration", "event.end", "event.hash", "event.id",
    "event.ingested", "event.kind", "event.module", "event.original",
    "event.outcome", "event.provider", "event.reason", "event.reference",
    "event.risk_score", "event.risk_score_norm", "event.sequence",
    "event.severity", "event.start", "event.timezone", "event.type", "event.url",
    # source / destination / client / server base fields (entities also reuse
    # geo/user/as/nat by rule, handled in is_ecs)
    "source.address", "source.ip", "source.port", "source.domain",
    "source.bytes", "source.packets", "source.mac", "source.registered_domain",
    "source.top_level_domain", "source.subdomain",
    "destination.address", "destination.ip", "destination.port",
    "destination.domain", "destination.bytes", "destination.packets",
    "destination.mac", "destination.registered_domain",
    "client.address", "client.ip", "client.port", "client.domain", "client.bytes",
    "server.address", "server.ip", "server.port", "server.domain", "server.bytes",
    # host
    "host.architecture", "host.domain", "host.hostname", "host.id", "host.ip",
    "host.mac", "host.name", "host.type", "host.uptime",
    # network
    "network.application", "network.bytes", "network.community_id",
    "network.direction", "network.forwarded_ip", "network.iana_number",
    "network.name", "network.packets", "network.protocol", "network.transport",
    "network.type", "network.vlan.id", "network.vlan.name",
    # http
    "http.request.body.bytes", "http.request.body.content", "http.request.bytes",
    "http.request.id", "http.request.method", "http.request.mime_type",
    "http.request.referrer", "http.response.body.bytes",
    "http.response.body.content", "http.response.bytes", "http.response.mime_type",
    "http.response.status_code", "http.version",
    # url
    "url.domain", "url.extension", "url.fragment", "url.full", "url.original",
    "url.password", "url.path", "url.port", "url.query", "url.registered_domain",
    "url.scheme", "url.subdomain", "url.top_level_domain", "url.username",
    # user / user_agent
    "user.name", "user.id", "user.full_name", "user.email", "user.domain",
    "user.hash", "user.roles", "user.target.name", "user.target.id",
    "user.target.domain", "user.effective.name", "user.effective.id",
    "user.changes.name", "user.group.name", "user.group.id",
    "user_agent.original", "user_agent.name", "user_agent.version",
    "user_agent.device.name",
    # file
    "file.accessed", "file.created", "file.ctime", "file.device",
    "file.directory", "file.extension", "file.gid", "file.group", "file.inode",
    "file.mode", "file.mtime", "file.name", "file.owner", "file.path",
    "file.size", "file.type", "file.uid",
    # process
    "process.args", "process.command_line", "process.executable", "process.name",
    "process.pid", "process.ppid", "process.start", "process.title",
    "process.thread.id", "process.thread.name", "process.working_directory",
    "process.exit_code", "process.parent.pid", "process.parent.name",
    # tls
    "tls.cipher", "tls.version", "tls.version_protocol", "tls.established",
    "tls.resumed", "tls.curve", "tls.server.subject", "tls.client.subject",
    # dns
    "dns.question.name", "dns.question.type", "dns.question.class",
    "dns.response_code", "dns.type", "dns.id",
    # observer
    "observer.hostname", "observer.id", "observer.ip", "observer.mac",
    "observer.name", "observer.product", "observer.serial_number", "observer.type",
    "observer.vendor", "observer.version",
    # rule
    "rule.author", "rule.category", "rule.description", "rule.id", "rule.license",
    "rule.name", "rule.reference", "rule.ruleset", "rule.uuid", "rule.version",
    # vulnerability
    "vulnerability.category", "vulnerability.classification",
    "vulnerability.description", "vulnerability.enumeration", "vulnerability.id",
    "vulnerability.reference", "vulnerability.report_id",
    "vulnerability.scanner.vendor", "vulnerability.score.base",
    "vulnerability.score.environmental", "vulnerability.score.temporal",
    "vulnerability.score.version", "vulnerability.severity",
    # threat
    "threat.framework", "threat.indicator.ip", "threat.indicator.name",
    "threat.indicator.type", "threat.tactic.id", "threat.tactic.name",
    "threat.technique.id", "threat.technique.name", "threat.group.name",
    "threat.software.name",
    # email (ECS 8.x)
    "email.from.address", "email.to.address", "email.cc.address",
    "email.bcc.address", "email.reply_to.address", "email.sender.address",
    "email.subject", "email.message_id", "email.local_id", "email.direction",
    "email.delivery_timestamp", "email.origination_timestamp", "email.x_mailer",
    "email.content_type",
    # service / log / error
    "service.name", "service.id", "service.type", "service.version",
    "service.state", "service.node.name",
    "log.level", "log.logger", "log.origin.file.name", "log.syslog.priority",
    "log.syslog.facility.code", "log.syslog.severity.code",
    "error.code", "error.id", "error.message", "error.type", "error.stack_trace",
    # registry / dll / package (common host telemetry)
    "registry.key", "registry.path", "registry.value", "registry.hive",
    "dll.name", "dll.path",
    "package.name", "package.version", "package.type",
    # organization / cloud / container / agent
    "organization.id", "organization.name",
    "cloud.account.id", "cloud.availability_zone", "cloud.instance.id",
    "cloud.provider", "cloud.region", "cloud.service.name",
    "container.id", "container.image.name", "container.name", "container.runtime",
    "agent.id", "agent.name", "agent.type", "agent.version",
    # related (for correlation)
    "related.ip", "related.user", "related.hosts", "related.hash",
    # ecs
    "ecs.version",
}

# --- Common wrong/legacy names -> correct ECS (the autocorrect dictionary).
#     Keys may be bare tokens (a regex group name people might map directly) or
#     full dotted paths. Everything here is treated as an ERROR with the fix. ---
ALIASES = {
    # source / destination ip & port
    "srcip": "source.ip", "src_ip": "source.ip", "source_ip": "source.ip",
    "sourceip": "source.ip", "clientip": "source.ip", "client_ip": "source.ip",
    "ipaddress": "source.ip", "remote_addr": "source.ip", "remoteip": "source.ip",
    "dstip": "destination.ip", "dst_ip": "destination.ip",
    "destip": "destination.ip", "dest_ip": "destination.ip",
    "destination_ip": "destination.ip",
    "srcport": "source.port", "src_port": "source.port", "sport": "source.port",
    "dstport": "destination.port", "dst_port": "destination.port",
    "dport": "destination.port",
    # http
    "status": "http.response.status_code", "statuscode": "http.response.status_code",
    "status_code": "http.response.status_code",
    "response_code": "http.response.status_code", "httpstatus": "http.response.status_code",
    "method": "http.request.method", "verb": "http.request.method",
    "http_method": "http.request.method", "request_method": "http.request.method",
    "referer": "http.request.referrer", "referrer": "http.request.referrer",
    "http_referer": "http.request.referrer",
    "bytes_sent": "http.response.body.bytes", "body_bytes": "http.response.body.bytes",
    "resp_bytes": "http.response.body.bytes",
    # url
    "uri": "url.path", "request_uri": "url.path", "path": "url.path",
    "url": "url.original", "querystring": "url.query", "query_string": "url.query",
    # user / agent
    "username": "user.name", "user_id": "user.id", "uid": "user.id",
    "ua": "user_agent.original", "useragent": "user_agent.original",
    "user_agent": "user_agent.original", "http_user_agent": "user_agent.original",
    # host / time
    "hostname": "host.name", "host_name": "host.name", "msg": "message",
    "ts": "@timestamp", "time": "@timestamp", "timestamp": "@timestamp",
    "datetime": "@timestamp", "eventtime": "@timestamp",
    # network / proto
    "proto": "network.protocol", "protocol": "network.protocol",
    "transport": "network.transport",
    # vuln
    "cve": "vulnerability.id", "cve_id": "vulnerability.id",
    "cvss": "vulnerability.score.base", "cvss_base": "vulnerability.score.base",
    "cvss_score": "vulnerability.score.base",
    # severity / outcome
    "severity": "event.severity", "loglevel": "log.level", "level": "log.level",
    "action": "event.action", "outcome": "event.outcome", "result": "event.outcome",
    # --- project-specific migrations (these rules used a non-ECS name) ---
    "email.from": "email.from.address",
    "email.to": "email.to.address",
    "file.owner_uid": "file.uid",
    "vulnerability.cvss.base_score": "vulnerability.score.base",
}

# Typo detection only fires above this similarity, to avoid mislabelling a
# genuine custom field as a typo.
_TYPO_CUTOFF = 0.92


def strip_type(field):
    """Drop the '|int' / '|float' type suffix and whitespace."""
    return field.split("|", 1)[0].strip()


def is_ecs(field):
    f = strip_type(field)
    if not f:
        return False
    if f in RESERVED_ROOT or f in ECS_FIELDS:
        return True
    parts = f.split(".")
    # reuse: a reusable set name appears as a segment, suffix is a valid leaf,
    # and the field is rooted at an ECS entity / field set.
    for i, seg in enumerate(parts):
        if seg in REUSABLE:
            leaf = ".".join(parts[i + 1:])
            if (leaf == "" or leaf in REUSABLE[seg]) and (
                i == 0 or parts[0] in ECS_FIELD_SETS
            ):
                return True
    return False


def classify(field):
    """Return (status, suggestion).

    status: 'ecs'    -> valid ECS (suggestion None)
            'alias'  -> known wrong name (suggestion = correct ECS field)
            'typo'   -> very close to an ECS field (suggestion = that field)
            'custom' -> not ECS, no clean target (suggestion = soft hint or None)
    """
    f = strip_type(field)
    if is_ecs(f):
        return ("ecs", None)
    if f in ALIASES:
        return ("alias", ALIASES[f])
    near = difflib.get_close_matches(f, ECS_FIELDS, n=1, cutoff=_TYPO_CUTOFF)
    if near:
        return ("typo", near[0])
    # Under a real ECS set but unknown leaf, or a wholly custom namespace:
    # allowed as a custom field. Only offer a hint when it is a strong match,
    # so we never push a misleading "did you mean".
    hint = None
    soft = difflib.get_close_matches(f, ECS_FIELDS, n=1, cutoff=0.8)
    if soft:
        hint = soft[0]
    return ("custom", hint)


def suggest(field, n=5):
    """Ranked ECS fields a wrong/typo field probably meant."""
    f = strip_type(field)
    if f in ALIASES:
        return [ALIASES[f]]
    out = difflib.get_close_matches(f, ECS_FIELDS, n=n, cutoff=0.5)
    # also try the last segment (e.g. 'status' inside 'web.status')
    tail = f.split(".")[-1]
    if tail in ALIASES and ALIASES[tail] not in out:
        out.insert(0, ALIASES[tail])
    return out[:n]


def search(concept, n=12):
    """Find ECS fields by a plain word/concept ('country', 'http status', 'mail')."""
    w = concept.lower().strip()
    if not w:
        return []
    tokens = w.split()
    pool = sorted(ECS_FIELDS) + sorted(
        f"<entity>.{s}.{leaf}" for s, leaves in REUSABLE.items() for leaf in sorted(leaves)
    )
    seen, res = set(), []

    def take(fields):
        for f in fields:
            if f not in seen:
                seen.add(f)
                res.append(f)

    # Best: fields containing ALL tokens. Then: any single token. Then: fuzzy.
    take(f for f in pool if all(t in f.lower() for t in tokens))
    if len(res) < n:
        take(f for f in pool if any(t in f.lower() for t in tokens))
    if len(res) < n:
        take(difflib.get_close_matches(tokens[-1], list(ECS_FIELDS), n=n, cutoff=0.6))
    return res[:n]
