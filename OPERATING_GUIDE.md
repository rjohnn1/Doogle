# Doogle Operating Guide

## 1. Local installation and environment check

```bash
git clone <your-github-repo-url> doogle
cd doogle
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[ai,dev]"     # or ".[dev]" without optional AI routing
doogle doctor
```

`doogle doctor` is read-only. It checks the Python version, where blueprints were found and that
they all validate, which tools the blueprints need (`gcloud`, `clasp`, `test`) and whether they are
on `PATH`, the active gcloud account and default project (local config only), whether Application
Default Credentials and clasp credentials exist (it checks the files exist, never reads them),
whether optional AI routing is configured (it never prints the key), and where state will be
written. Missing cloud tools are warnings, not failures, because you only need them to apply.

Tool setup:

| Tool | Install | Sign in |
|---|---|---|
| Google Cloud CLI | https://cloud.google.com/sdk/docs/install | `gcloud auth login` (and `gcloud auth application-default login` for local SDK runs) |
| Firebase CLI | `npm install -g firebase-tools` | `firebase login` |
| clasp | `npm install -g @google/clasp` | `clasp login`, then enable the Apps Script API at https://script.google.com/home/usersettings |

### Where blueprints come from

1. `--blueprints-dir PATH` (an error if it does not exist)
2. `$DOOGLE_BLUEPRINTS_DIR`
3. Blueprints bundled in the installed package (`pip install .` or a wheel)
4. The `blueprints/` folder of a source checkout (`pip install -e .`)

So `doogle` works from any directory. Point `--blueprints-dir` at your own folder to use private
blueprints; they get the same strict validation.

### Where state goes

`./.doogle/state.json` in the directory you run from, or `--state-dir` / `$DOOGLE_STATE_DIR`.
Run Doogle from your project folder so state lives next to the code it deployed. The folder is
git-ignored and contains its own `.gitignore`.

## 2. Safe planning, execution and teardown loop

1. **Discover**: `doogle list` or `doogle ask "<your situation>"`.
2. **Inspect trade-offs**: `doogle explain <id>`; read "Do NOT use it when" first.
3. **Dry run**: `doogle plan <id> --var key=value ...`. Read every command, the risk tags,
   `PUBLIC` flags, the verify checks and the teardown section.
4. **Apply with intention**: re-run with `--apply`. Type the blueprint id to confirm. Add
   `--approve-high-risk` only after reading the high-risk steps. Each step runs, then its verify
   check; the first failure stops the run.
5. **Recover**: `doogle history latest` shows what happened and the output tail of a failed step.
   Fix the cause and `doogle resume latest` (steps that already succeeded are skipped). Manual
   steps pause the run; resume once you have done them.
6. **Clean up**: `doogle teardown latest` shows the exact recorded commands;
   `doogle teardown latest --apply --approve-high-risk` runs them, continues past individual
   failures and records each result. Entries whose resources this run did not create (the step
   was skipped because the resource already existed, or never ran) show `WILL SKIP` with the
   reason and are not executed. Finish any `MANUAL` teardown entries by hand.
7. **If something goes wrong**: a failed step prints a one-line `hint:` for common causes (not
   logged in, billing disabled, API not enabled yet, project not found, permission denied, quota).
   Ctrl-C marks the run `interrupted`; `doogle resume latest` or `doogle teardown latest` both work
   afterwards. If `.doogle/state.json` is corrupted, Doogle stops with a clear error; the previous
   version is in `.doogle/state.json.bak`.

Variables can come from a file instead of flags:

```yaml
# vars.yaml  (do not commit if it holds anything sensitive)
project_id: my-project-123
region: europe-west1
```

`doogle plan solo-serverless-api --vars-file vars.yaml`

### Blueprint freshness

Google CLIs change. Every blueprint records `last_verified`, `verification_status`
(`simulated`, `syntax-checked`, `live`) and `verified_versions`. `doogle list` shows them and
warns about blueprints older than 180 days; `doogle list --stale` lists only those. Before
applying a stale blueprint, compare its commands (from `doogle plan`) with the official docs, or
run `python scripts/check_cli_syntax.py` with your installed CLIs (reads `--help` only).

Steps with a `skip_if` check are safe to re-run: if the resource already exists, the command is
skipped and the run record says so.

## 3. Driving Doogle from an agent

Coding agents and multi-agent tools should use structured output and exit codes, never scrape text.

```bash
doogle ask "<user goal>" --json            # -> .recommendation, .confidence, .matches[]
doogle explain <id> --json                 # -> .when_not_to_use, .variables, .steps
doogle plan <id> --var ... --json          # -> .steps[].command/argv/risk, .summary.warnings
doogle doctor --json                       # -> .status pass|warn|fail, .checks[]
doogle history --json
doogle list --stale --json                 # -> .stale[] ids not verified in 180 days
doogle share <id> --json                   # -> .badge_markdown, .markdown
```

| Exit code | Meaning | Agent should |
|---|---|---|
| 0 | Success (or run paused at a manual step; check `.status`) | Continue |
| 1 | A step or verify check failed | Read `steps[].output_tail`, report, maybe `resume` |
| 2 | Bad input: unknown blueprint, missing/invalid variable, invalid blueprint | Fix input; `error` field explains |
| 3 | Safety gate blocked: missing tool, high-risk not approved, no confirmation | Ask the human; nothing ran |

Rules for agents:

* Always show the human the `plan` output before any `--apply`.
* Pass `--yes` only after a human approved that exact plan; pass `--approve-high-risk` only when a
  human explicitly approved the high-risk steps listed in `summary.high_risk_steps`.
* Never run teardown, or any delete, without explicit human approval.
* Never put keys, tokens or service-account JSON in variables, prompts, commits or logs.

## 4. Operating rules for maintainers

* Never run unreviewed destructive commands (deleting projects, databases or buckets) automatically.
  The validator forbids project deletion outright and forces deletes to be high-risk.
* Keep `pytest` green before merging any PR or pushing a release tag. CI runs tests on Python
  3.10-3.13, `doogle validate`, `doogle docs --check`, a wheel build and an installed-wheel smoke test.
  CodeQL scans Python and workflows; a weekly freshness workflow reports stale blueprints and
  checks every command against the latest gcloud / firebase-tools / clasp `--help`.
* After editing any blueprint, run `doogle docs` and commit the regenerated files.
* Keep the non-affiliation note in README.md.
* Re-check blueprint commands against current official docs at least each release; Google CLIs
  rename flags over time.

## 5. Troubleshooting

| Problem | Fix |
|---|---|
| `could not locate blueprints` | Reinstall, or pass `--blueprints-dir` / set `DOOGLE_BLUEPRINTS_DIR` |
| `refusing to start: required tool(s) not found` | Install the tool shown and make sure it is on `PATH`; `doogle doctor` |
| `require --approve-high-risk` | Expected. Review the listed steps, then add the flag |
| `without confirmation in a non-interactive session` | Add `--yes` (after review), or run in a terminal |
| `does not match required pattern` | The value is malformed (e.g. project IDs are 6-30 lowercase letters, digits, hyphens) |
| `state file ... is not valid JSON` | Move `.doogle/state.json` aside; Doogle starts a fresh one |
| `blueprint's steps changed since this run was recorded` | The blueprint was edited; tear down the old run and start a new one |
| `--ai` says it used offline routing | Install `.[ai]`, set `GEMINI_API_KEY`, check network/quota |
| `doogle docs --check` fails in CI | Run `doogle docs` locally and commit `docs/DECISION_MATRIX.md` and `README.md` |
| `schema_version must be 2` | Your custom blueprint is v1; add `maintainer`, `last_verified`, `verification_status`, `verified_versions` |

## 6. Publishing this repository

```bash
git remote add origin https://github.com/OWNER/REPO.git
git push -u origin main
```
