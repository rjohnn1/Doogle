# Security policy

Doogle runs cloud CLI commands with your credentials, so we take its safety guarantees seriously.

## Reporting a vulnerability

**Please do not open a public issue.** Report privately through GitHub's
[private vulnerability reporting](https://github.com/rjohnn1/Doogle/security/advisories/new)
(Security tab > "Report a vulnerability"). Enable private vulnerability reporting under Settings > Code security if it is not already on.

Include what you found, how to reproduce it (ideally with `doogle plan` or stub CLIs, no real
cloud project needed), the impact, and the Doogle version. We aim to acknowledge within
5 working days and to agree a disclosure date with you; we will credit you in the advisory
unless you prefer otherwise. This is a volunteer project, so timelines are best effort.

## In scope

* Bypassing a safety gate: running a mutating step without `--apply`, a high-risk step without
  `--approve-high-risk`, or anything without confirmation / `--yes`.
* Command or argument injection through variables, vars files, state files or blueprints
  (for example a value that becomes a second argument, a flag, or a shell command).
* A blueprint that passes `doogle validate` but runs a destructive, forbidden or shell command.
* `plan`, `list`, `explain`, `ask` (offline), `doctor`, `docs`, `share` or `validate` making network
  calls or changing files/cloud state (other than `docs` writing its documented files).
* Secrets leaking into output, `.doogle/state.json`, generated docs or the AI routing prompt.
* Vulnerabilities in Doogle's GitHub workflows (e.g. untrusted input in `run:` steps).

## Out of scope

* Vulnerabilities in gcloud, firebase-tools, clasp, Google services or the Gemini API: report
  those to Google through its own programs.
* Risks of a blueprint you approved with `--approve-high-risk` working as documented (e.g. a
  public Cloud Run URL being public).
* Self-inflicted problems from editing the engine or writing your own unsafe blueprints outside
  the validator.

## Never post secrets

Do not put API keys, OAuth tokens, service-account JSON, `.clasprc.json`, `.env` files,
`doogle history` output with real project IDs, or emails in issues, discussions, PRs or logs. If
you leaked a credential, revoke or rotate it first (Google Cloud console > APIs & Services >
Credentials, or AI Studio's API keys page), then tell us if Doogle was involved.

## Supported versions

Only the latest minor release receives security fixes.
