# Using Doogle with AI coding agents

Doogle is a local CLI. You can run it from the terminal of an AI coding agent or IDE,
including [Google Antigravity](https://antigravity.google/), the same way you would run
`gcloud` or `pytest`. There is **no official Antigravity integration** and no Doogle
plugin; the agent just invokes commands in your workspace.

[Google Antigravity](https://antigravity.google/docs/getting-started/) is Google's
agentic development platform: a standalone IDE, a terminal CLI (`agy`), and official
extensions for editors such as VS Code. Its agents can edit files and run terminal
commands. The pattern below also applies to other coding agents that can run a shell.

## Safe loop

1. Install Doogle in the workspace (`pipx install "git+https://github.com/rjohnn1/Doogle.git"`
   or `pip install -e .` from a clone). Cloud tools such as `gcloud` are only needed when
   you apply a blueprint.
2. Prefer **`--json`** on every command so the agent can parse structured results instead of
   scraping terminal text, and always check the exit code (see
   [OPERATING_GUIDE.md](../OPERATING_GUIDE.md#3-driving-doogle-from-an-agent)).
3. Start with `doogle ask "<goal>" --json`. Routing is offline by default. `doogle ask --ai`
   is optional and only contacts Gemini if you have set a key; it can choose among catalogued
   blueprints and never runs anything.
4. Inspect a candidate with `doogle explain <id> --json`, then **`doogle plan <id> --var ... --json`**.
   Plan is a dry run: it prints the exact commands and does not change your Google project.
5. Show that plan to a human before any apply. `--apply` requires typing the blueprint id to
   confirm (or `--yes` for scripted runs). Steps marked high-risk also need
   `--approve-high-risk`. Do not pass those flags unless a human has approved that exact plan,
   including every high-risk step.

```bash
doogle ask "add login and a database to my react web app" --json
doogle explain firebase-auth-mvp --json
doogle plan firebase-auth-mvp --var project_id=my-mvp-123 --json
# only after a human has reviewed the plan:
doogle plan firebase-auth-mvp --var project_id=my-mvp-123 --apply --approve-high-risk --yes
```

Exit code `3` means a safety gate blocked the run (missing tool, missing confirmation, or
high-risk not approved) and nothing was changed. Never run `doogle teardown` or any delete
without explicit human approval.

## What this is not

Doogle does not register itself as an Antigravity skill, MCP server, or IDE extension.
An MCP mode that exposes `list` / `explain` / `plan` as plan-only tools is on the
[roadmap](ROADMAP.md); until then, the terminal + `--json` workflow above is the supported
way to use Doogle from an agent IDE.
