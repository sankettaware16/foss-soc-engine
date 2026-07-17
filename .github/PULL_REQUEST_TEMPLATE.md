## Summary

<!-- What does this PR change, and why? Link the related issue: Fixes #NN -->

## Type of change

- [ ] New or updated parsing rule (`rules/*.yaml`)
- [ ] Engine / tools code
- [ ] Web UI or Kibana plugin
- [ ] Documentation
- [ ] CI / repository housekeeping

## Checklist

- [ ] Regression battery passes locally (`test_config.py --skip-kafka`, `test_timestamps.py`, `test_enrichment.py`, `test_golden.py`)
- [ ] For rule changes: `ecs_helper.py check` is clean and the golden sample (`tests/samples/<rule>/`) is added or intentionally refreshed (`test_golden.py --update <rule>`) with the diff reviewed
- [ ] No real log data anywhere (IPs, hostnames, usernames, org names are sanitized)
- [ ] `CHANGELOG.md` updated under `[Unreleased]` (for user-visible changes)
