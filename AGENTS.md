# Agent instructions for Doogle

This file is for AI coding agents (including [Google Antigravity](https://antigravity.google/),
which loads a root `AGENTS.md` or `GEMINI.md` as always-on workspace rules — see the
[Antigravity rules guide](https://antigravity.google/docs/rules/)). This repo uses `AGENTS.md`.

Two jobs, two sections. Longer walkthrough: [docs/USING_WITH_AGENTS.md](docs/USING_WITH_AGENTS.md).
Exit codes and JSON field notes are also in [OPERATING_GUIDE.md](OPERATING_GUIDE.md#3-driving-doogle-from-an-agent).

## A. Using Doogle in a user's project

Doogle is a local CLI. There is no official Antigravity (or other IDE) plugin; run commands in the workspace terminal.

**Install** (Python 3.10+). Cloud CLIs (`gcloud`, `firebase`, `clasp`) are only needed when a human later applies a plan:

```bash
pipx install "git+https://github.com/rjohnn1/Doogle.git"
# or: pip install "git+https://github.com/rjohnn1/Doogle.git"
doogle doctor --json    # read-only: Python, blueprints, tools, sign-in; .status is pass|warn|fail
```

**Safe loop — dry-run only until a human approves an exact plan:**

```bash
doogle ask "<user goal>" --json
doogle explain <id> --json
doogle plan <id> --var key=value --json    # default is dry-run; nothing in the Google project changes
```

- Always pass `--json` and read structured stdout. Do not scrape human text.
- Always show the human the `plan` output before any apply.
- **Never** pass `--apply`, `--yes` / `-y`, or `--approve-high-risk` unless the human has explicitly approved that exact plan, including every high-risk step. `--apply` executes; `--yes` skips the "type the blueprint id" confirmation; `--approve-high-risk` is required for IAM, public endpoints, and deletes.
- `doogle ask` is offline by default and never runs commands. `doogle ask --ai` is optional (needs `doogle[ai]` and `GEMINI_API_KEY`) and can only pick a catalogued blueprint.

**Reading JSON**

| Command | Useful fields |
|---|---|
| `ask` | `.recommendation`, `.confidence`, `.matches[]`, `.next_steps` |
| `explain` | `.when_not_to_use`, `.variables`, `.steps`, `.freshness` |
| `plan` | `.steps[].command` / `.argv` / `.risk`, `.summary.high_risk_steps`, `.summary.warnings`, `.summary.missing_tools` |
| `doctor` | `.status` (`pass`/`warn`/`fail`), `.checks[]` (`.name`, `.status`, `.detail`) |
| `history` / `teardown` | use `--json`; `teardown` without `--apply` only prints what would be undone |

**Exit codes:** `0` success (or paused at a manual step — check `.status`); `1` a step or verify failed (read `steps[].output_tail`); `2` bad input (`.error`); `3` a safety gate blocked the run and **nothing changed** — ask the human.

**Teardown.** `doogle teardown <run-id|latest> --json` is a dry run. It only undoes steps that run actually created. Do not pass `--apply` (or `--yes` / `--approve-high-risk`) without explicit human approval. The same rule applies to `doogle resume`.

Never put keys, tokens, or service-account JSON in variables, prompts, commits, or logs.

## B. Contributing to this repository

**Dev setup**

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"          # add ,ai for optional Gemini routing: ".[ai,dev]"
```

**Before you open a PR**, from the repo root:

```bash
pytest
ruff check .
mypy --strict src
doogle docs --check
doogle validate
```

After editing a blueprint, run `doogle docs` (regenerates `docs/DECISION_MATRIX.md` and the README gallery) and commit those files. New blueprints start from `blueprints/_template/`. Follow [CONTRIBUTING.md](CONTRIBUTING.md).

**Blueprint schema v2 (enforced by `doogle validate`)**

- `schema_version` must be `2`. `id` is kebab-case and must equal the folder name. Required metadata includes `maintainer`, `last_verified` (`YYYY-MM-DD`, not in the future), `verification_status`, and `verified_versions`.
- Be honest about verification. `simulated` = not run against real CLIs or a project; `syntax-checked` = commands checked against CLI `--help` (see `scripts/check_cli_syntax.py`) with real versions pinned; `live` = actually run on a throwaway project. Do not raise the status without the matching evidence. Record how you checked in `verification_notes`.
- **No shell execution.** One tool invocation per step: no `|`, `&&`, `;`, redirects, `$( )`, backticks, or `${}`. Commands never go through a shell; values are substituted inside tokens and not re-split.
- **Allow-listed tools only:** `gcloud`, `gsutil`, `bq`, `firebase`, `clasp`, `curl`, `test`. Adding a tool is an engine change (`ALLOWED_BINARIES` in `src/doogle/validator.py`), not a blueprint change.
- Deletes and IAM unbinds are `risk: high`. Automated teardown entries must set `undoes: [step ids]` so teardown cannot remove anything this run did not create. `verify_command` and `skip_if` must be read-only. No exact prices. No `TODO`s in `blueprint.yaml` or `GUIDE.md`. Never invent CLI flags; check `--help` or official docs.

Keep `engine.py` free of network calls outside `route_ai`. Doogle's only runtime dependency is PyYAML; keep it that way. Safety-model changes need two maintainer approvals (see [GOVERNANCE.md](GOVERNANCE.md)).
