# Introduction for invited reviewers

This is a short brief for people reviewing the private `rjohnn1/Doogle` repository.
The longer story lives in the [README](../README.md); the evidence behind the
quality claims is in [QUALITY_REPORT.md](QUALITY_REPORT.md).

## What problem it solves

Google's builder ecosystem (Cloud Run, Firebase, Workspace and Apps Script,
AI Studio, Vertex AI, the Agent Development Kit, and more) is large, overlapping,
and documented in scattered guides. Wiring a small, sensible stack — and being
able to undo it — is slower than it should be.

Doogle ("Do the Google") is a Python CLI that turns a plain-English goal into
the smallest curated blueprint that fits, shows every command before anything
runs, and records a teardown for what *this run* created. It is a decision guide
plus dry-run-first setup blueprints, not a wrapper for every Google product and
not a replacement for Terraform or Pulumi.

Five blueprints ship in this release candidate:

- `solo-serverless-api` — source-to-Cloud-Run HTTP API, scale-to-zero
- `firebase-auth-mvp` — Hosting + Authentication + Firestore
- `workspace-sheet-webhook` — Google Sheet + Apps Script webhook via clasp
- `gemini-adk-service` — ADK agent on Cloud Run + Vertex AI
- `ai-studio-to-vertex` — move a Gemini prototype from an AI Studio API key to Vertex AI

## How to try it in about five minutes

You need Python 3.10+ and access to this private repo. Cloud CLIs (`gcloud`,
`firebase`, `clasp`) are **not** required for a dry run.

```bash
git clone https://github.com/rjohnn1/Doogle.git doogle && cd doogle
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
doogle doctor
doogle list
doogle ask "add login and a database to my react web app"
doogle explain firebase-auth-mvp
doogle plan firebase-auth-mvp --var project_id=my-mvp-123
```

Use a lowercase project ID from `gcloud projects list`, not a display name, and
not the literal `YOUR_PROJECT_ID` from the guides.

These commands are read-only: `list`, `explain`, `plan` (without `--apply`),
`ask` (offline), `doctor`, `history`, `docs`, `share` and `validate` never
touch the network or your cloud resources.

Please do **not** pass `--apply` against a real project unless you are following
[LIVE_TEST_PLAN.md](LIVE_TEST_PLAN.md) on a throwaway Google Cloud project with
billing and a budget alert. Dry-run is the default; `--apply` asks you to type
the blueprint id; high-risk steps also need `--approve-high-risk`.

## What feedback we want

Honest, specific notes help more than star-style praise. In particular:

- Is the README clear about what Doogle is and is not?
- Did `ask` / `explain` / `plan` choose a sensible blueprint and show commands
  you would actually run? Where did the wording or the output confuse you?
- Do the `when to use` / `when not to use` sections and the decision matrix
  match how you would choose a stack?
- Did `doogle doctor` or any error message miss a prerequisite, or print
  something that looked like a secret?
- Gaps: a blueprint you needed that is not here, a flag that has drifted, a
  step that would fail on a real project.
- Safety concerns of any kind. Please report those privately (see
  [SECURITY.md](../SECURITY.md)), not in a public issue.

A GitHub issue (bug, blueprint drift, or new-blueprint proposal) or a comment
back to the maintainer is perfect.

## Known limitations

This is **1.2.0rc1**, a release candidate, not a finished public launch.

- **No live Google Cloud verification yet.** All five blueprints are
  `syntax-checked` against recorded CLI versions and pass with stub CLIs.
  IAM propagation, quotas, org policies and real error text are untested.
  See [LIVE_TEST_PLAN.md](LIVE_TEST_PLAN.md).
- **Windows is untested.** An experimental CI job exists but is non-gating.
- **macOS was not run locally**; it is in the CI matrix only.
- **`doogle ask` eval author bias.** The 107-goal eval set was written by the
  same reviewer who tuned the router. The held-out figure (13/16 = 81% strict)
  is the honest estimate; real user goals should be added later.
- **Redaction is pattern-based.** A secret with an unknown shape can still
  reach an output tail. `.doogle/` is git-ignored and mode `0600` for that
  reason.
- **Schema v2 is a breaking change.** v1 blueprints are rejected; they need
  `maintainer`, `last_verified`, `verification_status` and `verified_versions`.

Doogle collects no telemetry. It is an independent community project, not
affiliated with or endorsed by Google LLC.
