# Parsing rules

One YAML file per log source. The engine loads every `*.yaml` here and picks up
changes within about 10 seconds, no restart.

**Add or improve a parser:** follow
[CONTRIBUTING.md → Add a new parser](../CONTRIBUTING.md#add-a-new-parser) or
[Update an existing parser](../CONTRIBUTING.md#update-an-existing-parser).
Every rule here has a golden sample in
[`tests/samples/<pattern_name>/`](../tests/samples/), and `python3 test_all.py`
checks all of them.

**Rule syntax** (strategies, mapping, `timestamp:`, `source:`): see
[docs/writing-rules.md](../docs/writing-rules.md).

List the shipped rules with their source names:

```bash
grep -H '^source:' rules/*.yaml
```
