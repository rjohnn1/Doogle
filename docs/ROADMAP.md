# Roadmap

Doogle stays small and reliable: the top 10-15 builder patterns done well, with honest
verification. Dates are intentions, not promises. Track live status in the GitHub Project board.
<!-- TODO(maintainer): link the GitHub Projects board here once created. -->

## Now (v1.1)

- [x] Five blueprints: Cloud Run API, ADK agent on Vertex AI, Sheet webhook, Firebase Auth MVP, AI Studio -> Vertex AI
- [x] Freshness metadata, stale warnings, weekly freshness + CLI syntax workflow
- [x] Idempotent steps (`skip_if`), generated decision matrix and README gallery
- [x] Community kit: templates, governance, security policy, launch playbook

## Next (v1.2)

- [ ] Raise every shipped blueprint to `live` on throwaway projects, with published evidence
- [ ] Blueprints: Cloud Scheduler + Cloud Run job; Firebase Hosting preview channels in CI;
      BigQuery scheduled query to Sheets; Pub/Sub fan-out event bus; Gemini in Apps Script
- [ ] Streamed command output during `--apply` (long builds currently look silent)
- [ ] Redact secrets-looking strings from saved output tails
- [ ] Built-in cross-platform checks (`file_exists`, `http_ok`) instead of `test` / `curl`
- [ ] State file locking for concurrent runs

## Later

- [ ] MCP server mode so AI assistants can call `list`/`explain`/`plan` as tools (plan-only by default)
- [ ] Composable blueprints (an "API + database + auth" stack from smaller pieces)
- [ ] Drift detection before resume (compare recorded resources with current state, read-only)
- [ ] Eval set of goals -> expected blueprint for `doogle ask`, run in CI
- [ ] Localised guides (community-maintained)

## Not planned

- Wrapping every Google API, or replacing Terraform/Pulumi for full infrastructure-as-code
- Telemetry of any kind
- Automating project deletion, billing or organisation changes
- An LLM generating commands at runtime; models may only choose among reviewed blueprints

Want something here? Open a "New blueprint proposal" or start a Discussion.
