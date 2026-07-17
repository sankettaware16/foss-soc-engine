# Contributing to TLSOC Engine

Thank you for helping improve the TLSOC Engine. Parser rules, bug fixes,
documentation, and features are all welcome. Ecosystem-wide guidelines live in
[tlsoc — CONTRIBUTING](https://github.com/sankettaware16/tlsoc/blob/main/CONTRIBUTING.md);
this file covers what is specific to the engine.

## Ground rules

- Be respectful — participation is governed by the
  [Code of Conduct](CODE_OF_CONDUCT.md).
- **Never include real log data** in issues, commits, rules, or golden samples:
  no real IP addresses, hostnames, organization names, usernames, or email
  addresses. Sanitize samples (`203.0.113.10`, `user@example.com`,
  `example_org`).
- Security vulnerabilities go through [SECURITY.md](SECURITY.md), never public
  issues.
- Contributions are accepted under the [Apache-2.0 license](LICENSE).

## Contributing a parsing rule

The most valuable contribution — and the safest to accept, because CI guards it:

1. Write the rule following [docs/writing-rules.md](docs/writing-rules.md)
   (the AI master prompt in §10 can produce a first draft from sanitized log
   samples).
2. Validate the ECS fields:
   `python3 ecs_helper.py check rules/<name>.yaml`.
3. Add a **golden sample**: `tests/samples/<rule>/input.log` (sanitized lines)
   and refresh the expected output with
   `python3 test_golden.py --update <rule>`, then review the generated
   `expected.ndjson` by hand.
4. Run the full battery locally:

   ```bash
   python3 test_config.py --skip-kafka
   python3 test_timestamps.py
   python3 test_enrichment.py
   python3 test_golden.py
   ```

5. Open a pull request. CI re-runs the battery on Python 3.10 and 3.12; if your
   change alters any golden-sample answer, the build fails with a diff — that is
   by design.

## Contributing code

1. **Open an issue first** for anything beyond a small fix, so the approach can
   be agreed before you invest time.
2. Branch from the default branch; keep the diff focused on one topic.
3. Match the existing code style of the module you touch; avoid drive-by
   refactoring.
4. Run the regression battery (above) before pushing. For Web UI changes, also
   exercise the affected screens (`python3 webui/app.py`).
5. Open a PR with the template; describe what changed and why, and update
   `CHANGELOG.md` under `[Unreleased]` for user-visible changes.

## Documentation contributions

READMEs and `docs/` follow the ecosystem
[standards](https://github.com/sankettaware16/tlsoc/blob/main/docs/ecosystem.md)
and [branding](https://github.com/sankettaware16/tlsoc/blob/main/docs/branding.md).
Long-form content belongs in `docs/`; the README stays an overview.

## Development setup

```bash
git clone https://github.com/sankettaware16/tlsoc-engine.git
cd tlsoc-engine
pip3 install -r requirements.txt
python3 test_config.py --skip-kafka    # everything static validates without Kafka/Redis
```

The tools in [docs/development.md](docs/development.md) (`test_rules.py`,
`test_file.py`, `replicate.py`) all run with no infrastructure — you only need
Kafka/Redis to test live consumption.
