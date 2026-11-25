## Summary

<!-- What does this PR change, and why? Link the related issue: Fixes #NN -->

## Type of change

- [ ] New parser (`rules/<name>.yaml` + `tests/samples/<pattern_name>/`)
- [ ] Change to an existing parser
- [ ] Engine / tools code
- [ ] Web UI or Kibana plugin
- [ ] Documentation
- [ ] CI / repository housekeeping

## Checklist

- [ ] `python3 test_all.py` passes locally (CI runs the same checks)
- [ ] No real log data anywhere: IPs, hostnames, usernames, e-mail addresses,
      serial numbers and organization names are replaced with placeholders
- [ ] `CHANGELOG.md` updated under `[Unreleased]` (for user-visible changes)

### For parser changes

- [ ] `python3 ecs_helper.py check rules/<name>.yaml` is clean (`~` custom fields are fine)
- [ ] Golden sample added or refreshed with `python3 test_golden.py --update <pattern_name>`,
      and I read the `expected.ndjson` diff line by line
- [ ] `python3 elasticsearch/generate_template.py` run and the template committed
- [ ] The sample includes at least one line the rule must **not** match

**Where was this parser tested?** <!-- e.g. "a week of live FortiOS 7.4 traffic logs",
"vendor docs only", which log types were seen on real data and which were not -->
