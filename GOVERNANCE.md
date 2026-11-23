# Governance

Doogle is a small, independent, community-run project. This document says who decides what, and
how a blueprint earns trust.

## Roles

| Role | Who | Can |
|---|---|---|
| Contributor | Anyone who opens an issue, discussion or PR | Propose changes; review others' PRs |
| Blueprint maintainer | The handle in a blueprint's `maintainer` field | Is asked to review changes to that blueprint; keeps it fresh |
| Maintainer | Listed below and in `.github/CODEOWNERS` | Merge PRs, triage, cut releases, manage labels |
| Lead maintainer | One maintainer, listed below | Final call when consensus fails; security contact; trademark/naming questions |

Current maintainers: `@rjohnn1` (lead).

Contributors who make regular, high-quality contributions (for example several merged blueprints
or reviews over a few months) may be invited to become maintainers by consensus of existing
maintainers. Maintainers inactive for 6 months may be moved to emeritus, with thanks.

## Decision-making

* **Lazy consensus** for most changes: a PR with one maintainer approval and green CI can merge
  after it has been open at least 48 hours for anything non-trivial.
* **Two maintainer approvals** for changes to the safety model (`engine.py` gates, `validator.py`
  rules, execution or state code), CI workflows, or this document.
* Disagreements are discussed in the PR or a Discussion; if unresolved, the lead maintainer
  decides and writes down why.
* Larger direction changes go through a Discussion labelled `proposal` and the roadmap.

## Non-negotiables

These are not changed by a normal PR: dry-run by default; explicit approval for mutation and for
high-risk steps; no shell execution; no telemetry; no secrets in the repository; the
non-affiliation notice; never automating project deletion.

## How a blueprint gets its verification status

Every blueprint declares `verification_status`, `last_verified` and `verified_versions`. The
validator enforces the format; maintainers enforce honesty.

| Status | Means | Evidence required in the PR |
|---|---|---|
| `simulated` | Plan renders and the apply/verify/teardown flow runs against stub CLIs in tests | Passing tests |
| `syntax-checked` | Every subcommand and flag exists in the recorded CLI versions | Output of `python scripts/check_cli_syntax.py` with those versions installed; `verified_versions` must pin every CLI used |
| `live` | Run end to end, verify checks passed, and teardown succeeded, on a real throwaway project | A short log of `doogle plan ... --apply`, `doogle history latest`, and `doogle teardown latest --apply` with project IDs redacted; reviewed by a maintainer |

Rules:

* `last_verified` is the date of that evidence, not the date of the PR.
* Lowering the bar is always fine (for example `live` -> `syntax-checked` when a flag changes).
* A blueprint with `last_verified` older than 180 days shows as **stale** in `doogle list` and in
  the weekly freshness workflow; if nobody re-verifies it within a further 90 days, maintainers
  may mark it deprecated in its `tagline` and remove it in the next minor release.
* Nobody may claim `live` for a run they did not perform.

## Releases

Semantic versioning: patch for fixes and blueprint drift updates, minor for new blueprints,
commands or schema fields, major for breaking CLI or schema changes. Every release updates
CHANGELOG.md and is tagged `vX.Y.Z` from a green `main`.

## Trademarks and naming

"Google", "Firebase", "Gemini" and related names belong to Google LLC. Doogle uses them only to
describe compatibility, keeps the non-affiliation notice in README and CLI help, and does not use
Google logos or brand colours in its own branding.
