## What does this change?

<!-- One or two sentences. Link the issue: Fixes #123 -->

## Type

- [ ] New blueprint
- [ ] Blueprint fix / drift update
- [ ] CLI / engine change
- [ ] Docs / community

## Checklist

- [ ] `pytest -v` passes
- [ ] `doogle validate` reports 0 invalid
- [ ] `doogle docs` was run and the regenerated `docs/DECISION_MATRIX.md` / README gallery are committed (`doogle docs --check` passes)
- [ ] No secrets, tokens, service-account keys, personal project IDs or emails anywhere (use `YOUR_PROJECT_ID` placeholders)
- [ ] Every blueprint touched has a `teardown` that removes what it creates (or says what to remove by hand)
- [ ] `when_not_to_use` names at least one simpler or cheaper option
- [ ] Mutating steps have a `verify_command`; re-runnable steps have a `skip_if` where possible
- [ ] `last_verified`, `verification_status` and `verified_versions` are honest (see GOVERNANCE.md)
- [ ] CHANGELOG.md updated under "Unreleased" (for user-visible changes)

## How was it tested?

<!-- e.g. "plan + stub CLIs only", "scripts/check_cli_syntax.py with gcloud X.Y", "ran end to end on a throwaway project, then teardown" -->

## AI assistance

- [ ] Parts of this PR were written with an AI assistant, and I reviewed and tested them myself.
