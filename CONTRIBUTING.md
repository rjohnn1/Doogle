# Contributing to Doogle

Thanks for helping make the Google ecosystem as easy as A-B-C. The most valuable contribution is a
new, honest blueprint.

## Dev setup

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pytest -v
doogle validate
```

## Adding a blueprint (one pull request)

1. Create `blueprints/<your-id>/blueprint.yaml` and `blueprints/<your-id>/GUIDE.md`.
2. `doogle validate` and `doogle plan <your-id> --var ...` must succeed.
3. Add a routing case to `tests/test_engine.py::test_offline_routing` if your keywords overlap others.

### Schema (v1)

| Field | Type | Rule |
|---|---|---|
| `schema_version` | int | Must be `1` |
| `id` | string | Lowercase kebab-case; **must equal the folder name** |
| `name`, `category`, `tagline` | string | Non-empty; tagline is one line of value |
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

Step fields: `id` (kebab-case, unique), `description`, `command`, `risk` (`low`/`medium`/`high`,
default `low`), `public_exposure` (bool), `verify_command`, `verify_expect` (substring the verify
output must contain), `timeout_seconds`, or `manual: true` (instructions only, no command; the run
pauses until the user confirms and resumes).

Teardown fields: `id`, `description`, `command`, `risk` (default `high`), or `manual: true`.

### Command rules (enforced by the validator)

* One tool invocation per step. No `|`, `&&`, `;`, redirects, `$( )`, backticks or `${}`.
* The first word is a bare tool name (`gcloud`, `clasp`, `firebase`...), never a path, a variable,
  `sudo`, `rm` or a shell (`bash -c` is rejected).
* Quote arguments a shell would choke on, e.g. `'--format=value(status.url)'`.
* `{{placeholders}}` must be declared variables. Values are substituted *inside* tokens and never
  re-split, so `--member serviceAccount:{{sa}}@{{project_id}}.iam.gserviceaccount.com` is safe.
* Commands containing `delete`, `undeploy`, `remove-iam-policy-binding`, `destroy` or `purge`
  must be `risk: high`. `gcloud projects delete`, `gcloud organizations ...` and
  `gcloud billing accounts ...` are never allowed.
* `verify_command` must be read-only.
* Pass `--project {{project_id}}` explicitly instead of changing the user's gcloud config.

### Risk guidance

| Risk | Examples |
|---|---|
| low | Read-only checks, local file checks |
| medium | Enabling APIs, creating private resources, building images |
| high | IAM changes, anything public (`--allow-unauthenticated`, `ANYONE_ANONYMOUS`), any delete |

Mark every step that creates an internet-reachable endpoint `public_exposure: true`.

### GUIDE.md checklist

* A "Is this the right blueprint?" table with cheaper/simpler alternatives.
* Minimal working sample code if the blueprint deploys user code.
* How to test the result, cost guard-rails, a troubleshooting table, teardown.
* Placeholders like `YOUR_PROJECT_ID`, never real project IDs, emails or keys.

## Pull request checklist

- [ ] `pytest -v` is green and `doogle validate` reports 0 invalid
- [ ] Commands use current, documented CLI syntax (link the docs in `docs:`)
- [ ] `when_not_to_use` names at least one simpler or cheaper option
- [ ] Every mutating step has a `verify_command` where one exists
- [ ] Teardown removes everything the steps create, or says what to remove by hand
- [ ] No secrets, credentials, personal project IDs or exact prices
- [ ] Tested against a real throwaway project (say so in the PR description)

## Code changes

Keep `engine.py` free of network calls outside `route_ai`, keep `plan` pure, and add a test for
every new safety rule. Doogle has one runtime dependency (PyYAML); please keep it that way.
