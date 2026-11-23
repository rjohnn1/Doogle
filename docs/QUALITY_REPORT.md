# Quality report: Doogle 1.2.0rc1

An independent, adversarial review run on 2026-11-19 against v1.1.0 (commit `61c7b09`), with the
fixes on branch `hardening`. The reviewer assumed the tool was broken and tried to prove it. Every
number below comes from a command run during the review. Where something was **not** verified,
this report says so.

## Scorecard

| Area | Before (v1.1.0) | After (1.2.0rc1) | Verdict |
|---|---|---|---|
| Tests | 187 | **374** (property, fuzz, SIGINT, concurrency, mutation-driven, CLI) | strong |
| Line / branch coverage | 85.8% / 76.1% | **96.1% / 92.4%** | strong |
| `cli.py` coverage | 69.9% lines / 55.1% branches | 95.6% lines (94% combined) | good |
| Mutation score, safety paths ¹ | 66.1% (660/998) | **86.0%** (887/1031) | good |
| Mutation score, all of engine + validator | 62.9% (2555/4063) | 66.6% (2791/4188) | fair, see below |
| `doogle ask` eval (107 goals) | 86.8% on the first 91 | 93.5% overall; **held-out 13/16 = 81%** | fair |
| ruff (E,F,W,I,B,UP,S,SIM,RUF,C90,PLE,PLW,PERF,DTZ) | 94 findings | 0 | clean |
| mypy `--strict` | 75 errors | 0 | clean |
| bandit (`src`, `scripts`) | 7 low | 0 (each false positive annotated with a reason) | clean |
| pip-audit (project deps) | no known vulnerabilities | no known vulnerabilities | clean |
| gitleaks (history + tree) | no leaks | no leaks | clean |
| actionlint | clean | clean (new matrix + lint job) | clean |
| Max cyclomatic complexity | 97 (`validate_blueprint_data`) | 49 (`doctor`) | improved |
| Python versions run | 3.13 | **3.10, 3.11, 3.12, 3.13, 3.14** all 374/374 | good |
| Windows | untested | **still untested** (experimental CI job, non-gating) | gap |
| Live Google Cloud runs | none | **none** | gap, see `LIVE_TEST_PLAN.md` |

¹ The safety paths are `check_value`, `render_argv`, `render_text`, `enforce_gates`,
`teardown_preview`, `_check_run_shape`, `redact`, `StateStore`, `Executor.resume`,
`Executor.teardown`, `_validate_command`, `_readonly_problems`, `recorded_argv_problems` and
`_is_destructive`. A sample of the 144 surviving mutants was reviewed by hand. They are
message-text or formatting changes (JSON indent, `"UTF-8"` vs `"utf-8"`, temp-file prefixes),
equivalent mutants (`zip(strict=…)` after an explicit length check, loop bounds that only add
empty slices), or event-payload details. None of them weakens a gate. The overall score stays at
67% because of `doctor` (290 survivors, all diagnostics wording), routing weights and validator
error strings. Those are not safety paths.

## Findings, ranked by severity (all fixed unless marked)

**Critical**
1. **Teardown deleted resources the run never created.** If the `(default)` Firestore database
   existed before the run, `skip_if` correctly skipped creating it, but teardown still deleted
   it. IAM-grant teardowns could likewise remove bindings that predated Doogle. *Fix:* every
   automated teardown entry now declares `undoes: [step ids]`, and an entry only runs when one of
   those steps actually executed. Grants gained `skip_if` checks. The preview shows `WILL SKIP`
   with the reason. Legacy v1.1 records still tear down as before.

**High**
2. **The state file was trusted.** Editing `.doogle/state.json` made `teardown` and `resume` run
   arbitrary argv (for example `bash -c …`). *Fix:* `resume` re-renders every command from the
   blueprint and refuses if `argv`, `verify_argv` or `skip_argv` differ. Expectations and risk
   always come from the blueprint. `teardown` rejects recorded argv that uses a tool outside the
   allow-list or a never-automated pattern.
3. **Validator bypasses:** `env bash -c`, `python3 -c` and `firestore:delete` marked low risk
   all passed validation, as did `verify_command`/`skip_if` commands that mutate state. *Fix:* a
   tool allow-list, read-only rules (including curl flags), and `disable` plus `firestore:delete`
   classified as destructive.
4. **Concurrent runs lost data:** with 80 parallel writers only 21 runs survived. *Fix:* an
   exclusive lock around read-modify-write, atomic fsynced writes and a `.bak` copy. Now 90 of 90
   survive.
5. **Secrets in output tails.** API keys, tokens and emails echoed by CLIs were stored in the
   state file. *Fix:* redaction before anything is printed or stored. Verified with runtime-built
   fake secrets so scanners stay quiet.
6. **Hostile input crashed the tool.** Recursive YAML aliases (RecursionError), binary files
   (UnicodeDecodeError), wrong types (TypeError) and malformed or truncated state (KeyError)
   produced tracebacks. Bidi characters, NEL and 10 MB variable values were accepted. *Fix:*
   alias-free size- and depth-capped loading, type guards, state-shape validation, and value
   rules. Hypothesis fuzzing now finds no traceback.

**Medium**
7. **Blueprint drift against current Google behaviour.** All five blueprints were reviewed by
   hand against the official docs, which found:
   - New Firebase projects no longer get a default Hosting site, so non-interactive deploys
     failed. Added a `hosting:sites:create` step.
   - Source deploys need `roles/run.builder` on newer organisations. Documented it in the
     prerequisites and troubleshooting.
   - The `run-sources-*` bucket and Artifact Registry images leak cost. Added a manual teardown
     note.
   - Deploying over an existing service replaces it. Documented.
8. **Ctrl-C left runs stuck in `running`.** *Fix:* the step and the run are marked
   `interrupted` and the tool exits 130. A real-SIGINT end-to-end test covers resume and teardown
   afterwards.
9. **State directory permissions were 0755.** Now 0700, and the state file is 0600.
10. **`check_cli_syntax.py` mislabelled problems.** Unknown firebase/clasp subcommands were
    reported as "flag not found". A test also showed the script could silently stop capturing
    `--help` output. Both are fixed and tested.

**Low (UX)**
11. **The fresh-user quickstart failed on its last line.** `YOUR_PROJECT_ID` fails the
    lowercase pattern, and the error printed a regex. *Fix:* a placeholder-aware message, a
    working example ID, and pipx/pip install instructions. The `OWNER/REPO`
    placeholder was later replaced with `rjohnn1/Doogle` before the private review push.
12. **Error messages for common mistakes.** Ten were reviewed: id typo, variable typo, missing
    variable, no gcloud, not logged in, wrong or non-existent project, bad pattern, no runs,
    corrupt state, non-interactive without `--yes`. Added "Did you mean …?", plus `hint:` lines
    for not-logged-in, billing, API-not-enabled, project-not-found, permission-denied and quota
    errors. Teardown failures now print their output.
13. **Exact run IDs could lose to a prefix match.** Fixed.

## Fresh-user test

The wheel and the sdist were each installed into clean virtualenvs in an empty directory, and
the README quickstart was followed word for word. Both installed, reported the right version,
and `doctor`, `list`, `ask`, `explain` and `plan` worked without any Google CLI. The friction
found is listed under item 11. The editable install showed stale metadata until it was
reinstalled. That only affects contributors.

## CI realism

`act` is installed but cannot run jobs here because there is no Docker daemon (`act -l` lists
the 7 jobs). Instead, a local emulator ran every `run:` step of the Linux jobs in a pristine
`git archive` copy, with a fresh uv virtualenv per matrix Python:
- `test` passed on 3.10, 3.11, 3.12, 3.13 and 3.14.
- `lint` passed.
- `package` (wheel and sdist from another directory) passed after one real CI bug, found by this
  emulation, was fixed.

**Not verified:**
- the macOS job;
- the Windows job;
- `actions/checkout`/`setup-python` behaviour;
- CodeQL;
- the scheduled freshness workflow on GitHub's runners.

## `doogle ask` evaluation

107 labelled goals live in `tests/data/ask_eval.yaml`, and `tests/test_ask_eval.py` enforces
these thresholds:
- tuned set strict ≥ 93%;
- all direct cases correct;
- out-of-scope ≥ 95%;
- held-out strict ≥ 75% and top-1 ≥ 80%.

The out-of-scope lexicon was tuned on this eval, so the held-out 81% is the honest estimate. The
remaining misses are generic "host my X server" phrasings and ties between `gemini-adk-service`
and `ai-studio-to-vertex`. All goals were written by the reviewer, so there is author bias. Real
user goals should be added after launch.

## What remains unverified (read before calling this "battle tested")

1. **No blueprint has run against real Google Cloud.** All five are `syntax-checked`: their
   flags match the CLIs and they work with stub CLIs. IAM propagation, quotas, org policies and
   real error text are untested. `docs/LIVE_TEST_PLAN.md` is the procedure to close this gap.
2. **Windows:** state locking falls back to `msvcrt`, POSIX-only tests are skipped, and nothing
   was run on Windows.
3. **macOS:** not run locally; it is in the CI matrix only.
4. **`doogle docs`** run from an installed wheel targets the packaged copy. It is meant to be a
   repo-only command and is documented that way, but it does not refuse.
5. **Mutation survivors** remain in non-safety code (`doctor`, routing, validator messages).
6. **Redaction is pattern-based.** A secret with an unknown shape can still reach an output tail.
   `.doogle/` stays git-ignored and 0600 for that reason.

## How to reproduce

```bash
pip install -e ".[dev]" ruff mypy types-PyYAML bandit pip-audit mutmut
pytest -q --cov=doogle --cov-branch                    # 374 passed
ruff check src scripts tests && mypy && bandit -q -r src scripts && pip-audit .
python scripts/check_cli_syntax.py                     # needs gcloud / firebase / clasp installed
DOOGLE_FUZZ_EXAMPLES=10 mutmut run --max-children 6 && mutmut results
```
