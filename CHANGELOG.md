# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Changed
- Default optional AI model for `doogle ask --ai` is now `gemini-4-hydrogen` (was
  `gemini-2.5-flash`). Vertex AI lists `gemini-2.5-flash` as retired on 2026-10-20;
  `gemini-4-hydrogen` is the current general-purpose generally-available model on both
  the Gemini API and Vertex AI. Override remains `DOOGLE_GEMINI_MODEL`. Blueprint
  guide examples were updated to match, with links to the official model lists.
- README status now describes this as a public release candidate (still `syntax-checked`,
  not yet live-verified).
- GitHub Actions: `google-github-actions/setup-gcloud` 2 → 3, `actions/setup-node` 4 → 7,
  `actions/setup-python` 5 → 7, `actions/checkout` 4 → 7 (Dependabot PRs #1–#4), and
  `github/codeql-action` 3 → 4 (PR #5 confirmed the bump, then Dependabot closed it after
  a rebase, saying the action was already up to date; main was still on v3).
- Repository URL and maintainer placeholders now use `rjohnn1/Doogle` and `@rjohnn1`.
- Code of Conduct enforcement contact points at GitHub private vulnerability reporting
  (no personal email).
- README states 1.2.0rc1 review status: blueprints are `syntax-checked`, not yet live-verified.
- Added `docs/INTRODUCTION.md` for invited reviewers.

### Added
- `docs/USING_WITH_AGENTS.md`: how to drive Doogle from an AI coding agent or IDE such as
  Google Antigravity, using `--json`, `doogle ask`, dry-run `plan`, and confirmed `--apply`.
  Linked from the README. No official Antigravity integration is claimed.
- Roadmap item for deeper agent-IDE integration.

### Security
- Enabled GitHub code scanning so the repo CodeQL workflow can upload results, plus
  secret scanning and push protection.

## [1.2.0rc1] - 2026-11-19

Hardening release candidate. See `docs/QUALITY_REPORT.md` for the evidence behind every item.

### Security / safety (fixed)
- **Teardown no longer deletes resources the run did not create.** Automated teardown entries
  must declare `undoes: [step ids]`; an entry runs only if one of those steps actually executed
  in this run (not skipped by `skip_if`, not never-reached). Previously, `firebase-auth-mvp`
  teardown could delete a pre-existing `(default)` Firestore database, and IAM-grant teardowns
  could remove bindings that existed before. `doogle teardown` shows `WILL SKIP` and why.
  Runs recorded by v1.1 (no `undoes` on disk) tear down exactly as before.
- The state file is no longer trusted: `resume` re-renders every command from the blueprint and
  refuses if `argv`, `verify_argv` or `skip_argv` on disk differ; expectations (`verify_expect`,
  risk, manual, timeouts) always come from the blueprint. `teardown` refuses recorded commands
  outside the tool allow-list or matching a never-automated pattern.
- Tool allow-list (`gcloud`, `gsutil`, `bq`, `firebase`, `clasp`, `curl`, `test`); `env bash -c`,
  `python3 -c` and similar are rejected. `verify_command`/`skip_if` must be read-only (no
  state-changing words; curl may not upload, write or change method). `disable` and
  `firestore:delete` count as destructive (must be `risk: high`).
- Variable values: max 512 chars; bidi/invisible/format characters, NEL/LS/PS and surrounding
  whitespace rejected.
- Output tails are redacted (API keys, OAuth access/refresh tokens, client secrets, JWTs,
  GitHub/Slack/`sk-` tokens, `Authorization` headers, `key=value` secrets, token query params,
  personal emails) before being printed or stored.
- `.doogle/` is `0700`, `state.json` `0600`; writes are atomic, fsynced, locked (concurrent
  `doogle` processes previously lost runs) and keep `state.json.bak`.
- Hostile YAML: aliases rejected (billion-laughs / recursion), 256 KiB and depth caps, binary
  files, wrong types and non-string keys give validation errors instead of tracebacks.

### Fixed
- Ctrl-C mid-step records the step and run as `interrupted` (exit 130) and both `resume` and
  `teardown` work afterwards.
- Malformed or truncated `state.json` gives a clean error naming the backup, never a traceback.
- `firebase-auth-mvp`: creates the default Hosting site explicitly (`firebase hosting:sites:create`)
  because new Firebase projects no longer get one automatically, so non-interactive deploys work.
- `solo-serverless-api`, `gemini-adk-service`: document the `roles/run.builder` requirement for
  source deploys, the `run-sources-*` bucket and Artifact Registry leftovers, and that deploying
  over an existing service of the same name replaces it.
- IAM grants in `gemini-adk-service` and `ai-studio-to-vertex` have `skip_if` checks so teardown
  only removes bindings Doogle added.
- `scripts/check_cli_syntax.py` reports unknown firebase/clasp subcommands as unknown commands
  (they print top-level help with exit 0) instead of "flag not found".
- An exact run id now wins over a prefix match.

### Added
- Actionable `hint:` lines for common failures (not logged in, billing disabled, API not enabled
  yet, project not found, permission denied, quota) on failed steps and teardown entries.
- "Did you mean ...?" for blueprint id and variable name typos; a specific message when a docs
  placeholder such as `YOUR_PROJECT_ID` is passed as a value.
- `doogle ask`: better offline routing (stop-words, synonyms, phrases, out-of-scope lexicon,
  `not_covered`, `confident`); a 107-goal eval is part of the test suite with thresholds.
- Tests: 187 -> 374 (Hypothesis property/fuzz tests, real SIGINT test, concurrency test,
  mutation-driven tests, human-readable CLI paths). CI: Python 3.10-3.14 + macOS, experimental
  Windows, ruff / mypy --strict / bandit / pip-audit job, sdist install check.
- `docs/QUALITY_REPORT.md` and `docs/LIVE_TEST_PLAN.md`.

### Changed
- README quickstart installs with `pipx`/`pip` from git or from a clone.

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

[Unreleased]: https://github.com/rjohnn1/Doogle/compare/v1.2.0rc1...HEAD
[1.2.0rc1]: https://github.com/rjohnn1/Doogle/compare/v1.1.0...v1.2.0rc1
[1.1.0]: https://github.com/rjohnn1/Doogle/compare/v1.0.0...v1.1.0
[1.0.0]: https://github.com/rjohnn1/Doogle/releases/tag/v1.0.0
