# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[Semantic Versioning](https://semver.org/).

## [Unreleased]

## [1.1.0] - 2026-11-19

### Added
- **Blueprints:** `firebase-auth-mvp` (Firebase Hosting + Authentication + Firestore) and
  `ai-studio-to-vertex` (Gemini API key prototype -> Vertex AI with ADC and a least-privilege
  service account). `doogle ask` routes to both.
- **Freshness metadata** (schema v2): required `maintainer`, `last_verified`,
  `verification_status` (`simulated` | `syntax-checked` | `live`) and `verified_versions`;
  optional `verification_notes`. `doogle list` and `doogle explain` show freshness and warn when
  a blueprint was last verified more than 180 days ago; `doogle list --stale [--stale-days N]`.
- **Idempotent steps:** optional read-only `skip_if` / `skip_if_expect` per step; matched checks
  skip the command and record why. Used for Firebase project, Firestore database, web app and
  service-account creation.
- `doogle docs [--check]`: deterministic `docs/DECISION_MATRIX.md` and README blueprint gallery
  generated from blueprints; CI fails when they are out of date.
- `doogle share <id>`: copy-pasteable "Built with a Doogle blueprint" badge and blurb.
- `src/doogle/validator.py`: schema and safety validation split out of the engine
  (`doogle.engine` re-exports it). New rules: freshness formats, honest status/version pinning,
  read-only `skip_if`, no `TODO` placeholders in blueprints or guides.
- `scripts/check_cli_syntax.py`: checks every gcloud/firebase/clasp subcommand and flag against
  the installed CLIs' `--help` (no network, no cloud calls). All five blueprints pass with
  gcloud 594.0.0, firebase-tools 15.37.0 and clasp 3.4.1.
- `blueprints/_template/` starter for "your first blueprint in 15 minutes".
- `doogle doctor` checks firebase-tools sign-in.
- Community and project health: CODE_OF_CONDUCT (Contributor Covenant 2.1), SECURITY, SUPPORT,
  GOVERNANCE, issue forms (bug, feature, new blueprint, blueprint drift), PR template, CODEOWNERS,
  Dependabot, CodeQL, labels, weekly freshness workflow, launch playbook, roadmap, VHS demo.

### Changed
- Blueprint `schema_version` is now **2**. v1 blueprints are rejected with a migration hint:
  add `maintainer`, `last_verified`, `verification_status` and `verified_versions`.
- `gemini-adk-service` skips service-account creation when it already exists.
- README: hero, badges, generated gallery, explicit "no telemetry" statement.

### Honesty notes
- No blueprint has yet been run against a live Google Cloud project; all are `syntax-checked`.

## [1.0.0] - 2026-11-19

### Added
- First release: `list`, `explain`, `plan` (dry run by default), `--apply` with confirmation,
  `--yes`, `--approve-high-risk`, `resume`, `teardown`, `history`, `ask` (offline + optional
  Gemini), `doctor`, `validate`; `.doogle/state.json` run records; strict schema validator;
  blueprints `solo-serverless-api`, `gemini-adk-service`, `workspace-sheet-webhook`.

[Unreleased]: https://github.com/OWNER/REPO/compare/v1.1.0...HEAD
[1.1.0]: https://github.com/OWNER/REPO/compare/v1.0.0...v1.1.0
[1.0.0]: https://github.com/OWNER/REPO/releases/tag/v1.0.0
