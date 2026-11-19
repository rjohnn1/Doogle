# Issue and PR labels

The canonical list lives in [`.github/labels.yml`](../.github/labels.yml). Create them once with
the GitHub CLI (after publishing):

```bash
# requires: gh auth login
python3 - <<'PY'
import subprocess, yaml
for l in yaml.safe_load(open(".github/labels.yml")):
    subprocess.run(["gh", "label", "create", l["name"], "--color", l["color"],
                    "--description", l["description"], "--force"], check=True)
PY
```

| Label | Use it for |
|---|---|
| `good first issue` | Small, well-described tasks a newcomer can finish in an afternoon (typo in a guide, a missing `skip_if`, a routing keyword). Include pointers to the files. |
| `help wanted` | Maintainers would welcome a PR; may need more context or cloud access. |
| `blueprint-request` | Proposals for new blueprints (from the "New blueprint proposal" template). |
| `drift` | A blueprint command, flag or guide step no longer matches the current Google CLI or product. |
| `bug` | Doogle CLI/engine misbehaviour. |
| `enhancement` | New features or improvements. |
| `safety` | Anything touching gates, validation, injection or secrets. Needs two maintainer approvals. |
| `docs` | Documentation only. |
| `dependencies` | Dependabot updates. |
| `needs-live-test` | A blueprint change that should be run on a throwaway project before merging. |
| `blueprint-of-the-week` | The blueprint featured in this week's community post. |
