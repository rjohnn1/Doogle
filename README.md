# Doogle: Do the Google Agent

**Say what you want to build. Doogle picks the right Google stack, shows you every command before
it runs, and leaves you a clean way to undo it.**

Google's developer ecosystem (Cloud Run, Firebase, Workspace and Apps Script, AI Studio, Vertex AI,
the Agent Development Kit, and more) is powerful but spread across hundreds of guides. Doogle
pairs plain-English routing with **deterministic, verifiable blueprints**:

```text
doogle ask "I want a cheap API for my side project"
  -> Recommended: solo-serverless-api  (confidence: high)
doogle explain solo-serverless-api        # when to use it, when NOT to, services, cost
doogle plan solo-serverless-api --var project_id=my-project-123            # dry run
doogle plan solo-serverless-api --var project_id=my-project-123 --apply --approve-high-risk
doogle teardown latest --apply --approve-high-risk                          # clean up
```

> **Non-affiliation note.** Doogle is an independent community project. It is not affiliated with or endorsed by Google LLC. Google, Google Cloud, Firebase, Gemini, Google Workspace and related
> marks are trademarks of Google LLC, used here only to describe compatibility.

## Decision matrix

| You want to... | Blueprint | Main services | Public by default? | Instead, consider first |
|---|---|---|---|---|
| Ship a small HTTP API / backend cheaply | `solo-serverless-api` | Cloud Run, Cloud Build, Artifact Registry | Yes (flagged high-risk) | Firebase Hosting for static sites; Cloud Functions for Firebase for a few triggers |
| Host an AI agent with tools on Gemini | `gemini-adk-service` | ADK, Vertex AI, Cloud Run, IAM service account | No (IAM-authenticated) | A direct `google-genai` call for single prompts; AI Studio to prototype; Vertex AI Agent Engine for managed sessions |
| Push webhooks / automation into a Google Sheet | `workspace-sheet-webhook` | Sheets, Apps Script web app, clasp | Yes, token-protected (flagged high-risk) | Google Forms for human input; Cloud Run + Firestore for volume |

Not sure? `doogle ask "<your situation>"` scores your words against every blueprint offline. Add
`--ai` to also ask Gemini (optional, see below); the AI can only pick from the catalog and never
runs anything.

## Quickstart

Requires Python 3.10+. Cloud tools are only needed when you apply a blueprint.

```bash
git clone <your-github-repo-url> doogle && cd doogle
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"          # add ,ai for optional Gemini routing: ".[ai,dev]"
doogle doctor                    # read-only checks: Python, blueprints, gcloud/clasp, auth context
doogle list
doogle ask "build an AI agent with Gemini that calls tools"
doogle explain gemini-adk-service
doogle plan gemini-adk-service --var project_id=YOUR_PROJECT_ID
```

`list`, `explain`, `plan`, `ask` (offline), `doctor`, `history` and `validate` never touch the
network or your cloud resources and work without gcloud installed.

### Commands

| Command | What it does |
|---|---|
| `doogle list [--category C]` | Browse blueprints |
| `doogle explain <id> [--guide]` | Trade-offs, anti-patterns, services, cost note, variables; `--guide` prints GUIDE.md |
| `doogle plan <id> --var k=v [--vars-file f.yaml]` | **Dry run**: resolved commands, risk tags, public-exposure flags, verify checks, teardown |
| `doogle plan <id> ... --apply` | Execute after confirmation (`--yes` for scripts, `--approve-high-risk` for high-risk steps) |
| `doogle resume <run-id\|latest>` | Continue a failed or paused run, skipping steps that succeeded |
| `doogle teardown <run-id\|latest> [--apply]` | Show, then run, the teardown recorded for that run |
| `doogle history [run-id]` | Runs recorded in `.doogle/state.json` |
| `doogle ask "<goal>" [--ai]` | Route a goal to a blueprint |
| `doogle doctor` | Read-only environment diagnostics |
| `doogle validate [--blueprints-dir D]` | Strict schema check (CI uses this) |

Every command except `resume` accepts `--json` for agents and scripts. Exit codes: `0` ok,
`1` runtime failure, `2` invalid input or blueprint, `3` blocked by a safety gate (nothing ran).

## Safety rules

1. **Dry-run is the default.** `plan` only prints. Nothing mutates without `--apply`.
2. **Explicit approval.** `--apply` asks you to type the blueprint id. Non-interactive sessions
   must pass `--yes`, or Doogle refuses.
3. **High-risk steps are blocked** (IAM changes, public endpoints, deletes) unless you also pass
   `--approve-high-risk`. This applies to `apply`, `resume` and `teardown`.
4. **Tools are checked up front.** If `gcloud` or `clasp` is missing, Doogle refuses before step 1.
5. **No shell, ever.** Templates are tokenised before variables are substituted, so a value always
   becomes exactly one argument. Values are pattern-checked, cannot start with `-`, cannot contain
   newlines, and any unresolved `{{placeholder}}` is an error. Displayed commands are shell-quoted.
6. **Verified, not assumed.** Steps can declare a read-only `verify_command` plus an expected
   output (`verify_expect`); exit code 0 alone is not success.
7. **Everything is recorded.** Each run writes step statuses, timestamps, exit codes, output tails,
   variables and the rendered teardown commands to `.doogle/state.json` after every step.
   `.doogle/` is git-ignored and gets its own `.gitignore`.
8. **No secrets in blueprints.** The validator rejects secret-looking variables (`api_key`,
   `token`, `password`...), exact price claims, shell operators, `sudo`/`rm`/shell interpreters,
   and never allows `gcloud projects delete`.

## Optional AI routing

```bash
pip install -e ".[ai]"
export GEMINI_API_KEY=...      # from Google AI Studio; never commit it
doogle ask --ai "I need a backend for a mobile app with a chat assistant"
```

Doogle sends only your goal and the blueprint catalog. Without a key, without the extra, or on
any API error, it falls back to offline routing and says so. Override the model with
`DOOGLE_GEMINI_MODEL`.

## For coding agents

Use `--json` everywhere, check exit codes, plan before apply, and never pass
`--approve-high-risk` without a human approving the exact plan. See
[OPERATING_GUIDE.md](OPERATING_GUIDE.md#driving-doogle-from-an-agent).

## Contributing

Adding a blueprint is one folder and one pull request: `blueprints/<id>/blueprint.yaml` plus
`GUIDE.md`. See [CONTRIBUTING.md](CONTRIBUTING.md). Wanted next: Firebase Hosting + Functions,
Firestore-backed web app, BigQuery scheduled report, Pub/Sub event bus, Gemini in Apps Script.

## License

Apache-2.0. See [LICENSE](LICENSE).
