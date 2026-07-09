# Elasticsearch index template

`soc-index-template.json` pre-defines the mapping for **every field the
engine can emit** (143 fields: ECS + the intentional custom fields), so the
first document can never decide a field's type wrong. Without it, one string
`"2"` makes `event.severity` text forever, a `null` poisons a field, and a
list-vs-scalar clash breaks the index for every later document.

## Load it (once, BEFORE the first event is indexed)

```bash
# adjust the URL / credentials to your cluster
curl -u elastic:$ELASTIC_PASSWORD -k -X PUT \
  "https://localhost:9200/_index_template/foss-soc" \
  -H "Content-Type: application/json" \
  --data-binary @elasticsearch/soc-index-template.json
```

Or paste it into Kibana → Dev Tools:

```
PUT _index_template/foss-soc
{ ...contents of soc-index-template.json... }
```

The template applies to indices matching **`soc-*`** — name your
Filebeat/agent output index accordingly (e.g. `soc-nginx_access`,
`soc-postfix`), or edit `index_patterns` before loading.

## What it guarantees

- `@timestamp`, `event.ingested`, `event.created` are real `date` fields.
- `source.ip` & friends are the `ip` type (searchable with CIDR ranges).
- `source.geo.location` is a `geo_point` (works on Kibana maps).
- Numeric fields (`event.severity`, ports, bytes, counters, scores) are
  numbers, even when a log delivers them as strings.
- Unknown future string fields become `keyword` (not analyzed `text`), via a
  dynamic template.
- `index.mapping.ignore_malformed` is on: one bad value drops that FIELD,
  never the whole document.

## Regenerating after rule changes

The template is **generated** — do not hand-edit it:

```bash
python elasticsearch/generate_template.py
```

The generator walks every `rules/*.yaml` (mapping targets, `|int`/`|float`
hints, static keys) plus the engine-added fields, and **refuses to write**
if two rules disagree about a field's shape (scalar vs object) — fix the
rule instead. New custom fields default to `keyword`; add an entry to
`TYPE_OVERRIDES` in the generator if a field needs a specific type.
