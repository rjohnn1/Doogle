# Contributing to Doogle

Thanks for helping make the Google ecosystem as easy as A-B-C. The most valuable contributions are
new, honest blueprints and drift fixes for existing ones. Please follow the
[Code of Conduct](CODE_OF_CONDUCT.md).

## Dev setup

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pytest -v
doogle validate
doogle docs --check
```

## Your first blueprint in 15 minutes

You need Python and this repo. You do **not** need a Google Cloud account to get a valid,
reviewable PR (it starts as `simulated`; maintainers or other contributors can raise it later).

1. **Pick a pattern (2 min).** Small, common, reversible: "Cloud Scheduler triggers a Cloud Run
   job", "Firebase Hosting preview channels for PRs", "BigQuery scheduled query to a Sheet".
   Check open `blueprint-request` issues first.
2. **Copy the template (1 min).**
   ```bash
   cp -r blueprints/_template blueprints/my-new-blueprint
   ```
3. **Let the validator guide you (8 min).** Edit `blueprint.yaml` and `GUIDE.md`, then run
   `doogle validate` until it reports 0 invalid. It names every remaining `TODO`, missing field
   and unsafe command. Copy command syntax from the official docs (link them in `docs:`).
4. **Look at your plan (2 min).**
   ```bash
   doogle plan my-new-blueprint --var project_id=demo-project-123
   doogle ask "the words a newcomer would type"     # should recommend your blueprint
   ```
   If `ask` picks something else, add `keywords`.
5. **Regenerate docs and test (2 min).**
   ```bash
   doogle docs        # updates docs/DECISION_MATRIX.md and the README gallery
   pytest -v
   ```
   Optionally add a case to `test_offline_routing` in `tests/test_engine.py`.
6. **Open the PR.** Fill in the checklist. Say honestly how you tested it.

Want `syntax-checked`? Install the CLIs you use and run `python scripts/check_cli_syntax.py`
(no network or cloud calls; it reads `--help` only). Put the versions it prints in
`verified_versions`. `live` needs a real throwaway project; see [GOVERNANCE.md](GOVERNANCE.md).

## Blueprint schema (v2)

| Field | Type | Rule |
|---|---|---|
| `schema_version` | int | Must be `2` |
| `id` | string | Lowercase kebab-case; **must equal the folder name** |
| `name`, `category`, `tagline` | string | Non-empty; tagline is one line of value |
| `maintainer` | string | GitHub handle or team that keeps it fresh |
| `last_verified` | date | `"YYYY-MM-DD"`, not in the future; date of the verification evidence |
| `verification_status` | string | `simulated`, `syntax-checked` or `live` (see GOVERNANCE.md) |
| `verified_versions` | map | Tool -> quoted version, e.g. `gcloud: "594.0.0"`, or `"unverified"`. `syntax-checked`/`live` must pin every CLI used |
| `verification_notes` | string | Optional; how it was verified, honestly |
| `keywords` | list[string] | Optional; words and phrases `doogle ask` matches |
| `when_to_use` | list[string] | At least one concrete good fit |
| `when_not_to_use` | list[string] | At least one anti-pattern **naming a simpler or cheaper Google alternative** |
| `services` | list[{name, role}] | Every moving part a user will see in their console |
| `estimated_cost` | string | Plain language (free tier vs. usage-based). **No exact prices**; link the official pricing page in `docs` |
| `prerequisites`, `docs` | list[string] | Optional; official documentation links |
| `requires_tools` | list[string] | Optional; tools are also inferred from each command's first word |
| `variables` | list[{key, prompt, default?, pattern?, example?}] | snake_case keys; no secrets; give a `pattern` for anything that ends up in a command |
| `steps` | list | At least one automated step (see below) |
| `teardown` | list | At least one entry with a `command` |

No string may contain `TODO`, and `GUIDE.md` must exist without `TODO`s.

Step fields: `id` (kebab-case, unique), `description`, `command`, `risk` (`low`/`medium`/`high`,
default `low`), `public_exposure` (bool), `verify_command` + `verify_expect` (substring the verify
output must contain), `skip_if` + `skip_if_expect` (read-only check; if it matches, the command is
skipped as already done, which makes re-runs safe), `timeout_seconds`, or `manual: true`
(instructions only; the run pauses until the user confirms and resumes).

Teardown fields: `id`, `description`, `command`, `risk` (default `high`), or `manual: true`.

### Command rules (enforced by the validator)

* One tool invocation per step. No `|`, `&&`, `;`, redirects, `$( )`, backticks or `${}`.
* The first word is a bare tool name (`gcloud`, `firebase`, `clasp`, `curl`...), never a path, a
  variable, `sudo`, `rm` or a shell (`bash -c` is rejected).
* Quote arguments a shell would choke on, e.g. `'--format=value(status.url)'`, `'--database=(default)'`.
* `{{placeholders}}` must be declared variables. Values are substituted *inside* tokens and never
  re-split, so `--member serviceAccount:{{sa}}@{{project_id}}.iam.gserviceaccount.com` is safe.
* Commands containing `delete`, `undeploy`, `remove-iam-policy-binding`, `destroy` or `purge`
  must be `risk: high`. `gcloud projects delete`, `gcloud organizations ...` and
  `gcloud billing accounts ...` are never allowed.
* `verify_command` and `skip_if` must be read-only.
* Pass `--project {{project_id}}` explicitly instead of changing the user's gcloud config.
* If a step cannot be automated honestly (console-only settings, browser logins), make it a
  `manual` step with exact instructions rather than a fragile workaround.

### Risk guidance

| Risk | Examples |
|---|---|
| low | Read-only checks, local file checks, registering a public web-app config |
| medium | Enabling APIs, creating private resources, building images |
| high | IAM changes, anything public (`--allow-unauthenticated`, `ANYONE_ANONYMOUS`, Hosting deploys), any delete |

Mark every step that creates an internet-reachable endpoint `public_exposure: true`.

### GUIDE.md checklist

* An "Is this the right blueprint?" table with cheaper/simpler alternatives.
* Minimal working sample code if the blueprint deploys user code.
* What is automated vs. manual, how to test the result, cost guard-rails, a troubleshooting table, teardown.
* Placeholders like `YOUR_PROJECT_ID`, never real project IDs, emails or keys.

## Fixing drift

Google CLIs change. If a flag moved: update the command, run `python scripts/check_cli_syntax.py`
with the new CLI version, update `verified_versions` and `last_verified`, run `doogle docs`, and
link the release notes in the PR.

## Pull request checklist

The [PR template](.github/PULL_REQUEST_TEMPLATE.md) has it: tests, `doogle validate`,
`doogle docs --check`, no secrets, teardown present, honest verification metadata.

## Code changes

Keep `engine.py` free of network calls outside `route_ai`, keep `plan` pure, keep validation in
`validator.py`, and add a test for every new safety rule. Doogle has one runtime dependency
(PyYAML); please keep it that way. Safety-model changes need two maintainer approvals
(see GOVERNANCE.md).

## AI-assisted contributions

Welcome, with disclosure: tick the box in the PR template, and make sure you have read, run and
understood everything you submit. Never let a model invent CLI flags; check them against
`--help` or the official docs.

## Recognition

We use [all-contributors](https://allcontributors.org) to thank everyone, including reviewers,
drift reporters, testers and doc writers, not just code authors.
