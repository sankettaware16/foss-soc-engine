# Changelog

All notable changes to TLSOC Engine are documented in this file.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and
this repository adheres to [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Changed

- Joined the unified TLSOC ecosystem: standardized README, branding, and
  cross-repository links. The project is now presented as **TLSOC Engine**
  (historically *FOSS SOC Engine*); service names (`foss-soc`, `foss-soc-ui`)
  are unchanged for compatibility with existing deployments.
- Documentation reorganized into `docs/`: `WRITING_RULES.md` →
  `docs/writing-rules.md`, `WEB_UI_GUIDE.md` → `docs/web-ui-guide.md`,
  `UPGRADING.md` → `docs/upgrading.md`, plus new installation, configuration,
  architecture, deployment, and development guides.

### Added

- **Internal IP map enrichment** — GeoIP for your own address space. A plain
  YAML file (`internal_ips.yaml`, or a directory of files; config block
  `internal_map:`) declares which subnet/range/IP is which building, room, lab
  or owner, and matching `source.ip`/`destination.ip` values are enriched with
  those fields (`geo.name` + custom `site.*`). Range syntax: CIDR,
  `a.b.c.d-a.b.c.d`, short `a.b.c.d-N`, single IPs, lists; overlapping ranges
  layer with most-specific-wins, resolved once at load into binary-searchable
  segments with an LRU in front (<1 % throughput cost at 5,000 entries; one
  boolean when disabled). Hot-reloads on edit with registry-style fail-safe;
  validated by `test_config.py`/`preflight.py` (ranges + the ECS field gate);
  new Web UI **IP Map** tab to edit, validate and test-look-up IPs; new
  regression suite `test_internal_map.py`. Ships disabled-by-content (empty
  starter map) with a worked example in `examples/internal_ips.example.yaml`.
- Community health files: contributing guide, security policy, code of conduct,
  issue templates, and a pull request template.
- Component roadmap (`docs/roadmap.md`).
