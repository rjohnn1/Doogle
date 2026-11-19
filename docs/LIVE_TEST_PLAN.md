# Live test plan: earning the `live` verification level

Every shipped blueprint is `syntax-checked` today: its commands and flags match the current CLIs'
`--help`, and the full plan / apply / verify / resume / teardown flow passes against stub CLIs.
**None has been run against real Google Cloud yet.** This document is the exact procedure for
doing that once per blueprint, in a disposable project, with a spending cap and proof that
nothing was left behind. Only a run that follows it may be used to set
`verification_status: live` (see `GOVERNANCE.md`).

Expected cost: cents. Everything used has a free tier; the budget alert below is the backstop.
Time: about 45 minutes per GCP blueprint, plus a few minutes for `workspace-sheet-webhook`.

> Conventions: `$P` is the disposable project ID, `$BA` your billing account ID, `$BP` the
> blueprint ID. Never paste real IDs, emails or URLs into issues or PRs: the evidence step below
> redacts them.

## 0. One-time setup (operator machine)

```bash
# An isolated gcloud configuration so nothing touches your normal setup.
export CLOUDSDK_CONFIG="$HOME/.config/gcloud-doogle-live"
gcloud auth login                         # a personal or test account with billing access
gcloud auth application-default login     # only needed for the ADK / Vertex blueprints
firebase login                            # firebase-auth-mvp only
clasp login                               # workspace-sheet-webhook only (use a test Google account)

pip install "doogle==1.2.0rc1"            # or an editable install of the commit under test
doogle --version && doogle doctor         # must be OK / WARN only, never FAIL
gcloud --version; firebase --version; clasp --version   # record these in the evidence
```

## 1. Create the disposable project with a budget alert

```bash
export BP=solo-serverless-api                       # one blueprint per project
export P="doogle-live-$(date +%m%d)-$RANDOM"        # <= 30 chars, lowercase
export BA=XXXXXX-XXXXXX-XXXXXX                      # gcloud billing accounts list

gcloud projects create "$P" --name="doogle live $BP" --labels=purpose=doogle-live-test
gcloud billing projects link "$P" --billing-account="$BA"

# Budget alert at 50% / 90% / 100% of USD 5 (emails go to billing admins by default).
gcloud services enable billingbudgets.googleapis.com --project "$P"
gcloud billing budgets create --billing-account="$BA" \
  --display-name="doogle-live-$P" --budget-amount=5USD \
  --filter-projects="projects/$P" \
  --threshold-rule=percent=0.5 --threshold-rule=percent=0.9 --threshold-rule=percent=1.0 \
  --billing-project="$P"
gcloud billing budgets list --billing-account="$BA" --billing-project="$P" \
  --filter="displayName=doogle-live-$P" --format="value(name)" > budget.name   # keep for cleanup
```

A budget alert does not stop spending. If an alert fires, stop and go to step 7 immediately.

## 2. Baseline inventory (before Doogle runs)

```bash
gcloud services enable cloudasset.googleapis.com --project "$P"
sleep 60   # the asset index needs a minute after enabling

mkdir -p evidence/$BP
gcloud asset search-all-resources --scope="projects/$P" \
  --format="value(assetType,name)" | sort > evidence/$BP/assets-before.txt
gcloud asset search-all-iam-policies --scope="projects/$P" --format=json > evidence/$BP/iam-before.json
gcloud projects get-iam-policy "$P" --format=json > evidence/$BP/project-iam-before.json
gcloud services list --enabled --project "$P" --format="value(config.name)" | sort > evidence/$BP/services-before.txt
```

## 3. Run the blueprint exactly as a user would

```bash
mkdir -p /tmp/doogle-live && cd /tmp/doogle-live        # empty dir; for ADK copy your agent here
doogle plan "$BP" --var project_id="$P" | tee ~/evidence/$BP/01-plan.txt
doogle plan "$BP" --var project_id="$P" --apply --approve-high-risk 2>&1 | tee ~/evidence/$BP/02-apply.txt
doogle history latest --json > ~/evidence/$BP/03-history.json
```

Blueprint-specific notes:

| Blueprint | Before apply | Manual steps during the run |
|---|---|---|
| `solo-serverless-api` | A tiny app in the current dir (any `main.py` + `requirements.txt` that serves `$PORT`, or a Dockerfile) | none |
| `gemini-adk-service` | `helper_agent/` with a Dockerfile (see its GUIDE) | none |
| `ai-studio-to-vertex` | none | ADC login, app config switch, runtime identity, key retirement: do them, type `done` |
| `firebase-auth-mvp` | none | enable sign-in providers in the console, add app files |
| `workspace-sheet-webhook` | Apps Script API enabled at script.google.com/home/usersettings | authorize-and-test in the browser |

Then prove the result works (commands are in each blueprint's GUIDE "Test it" section) and save
the output as `04-smoke.txt`. A run only counts if the deployed thing actually answers.

## 4. Robustness checks (same project, same run)

Each must behave as stated; record the output.

1. **Idempotency:** re-run the same `--apply` command in a fresh run. Steps with `skip_if` must
   report `already done, skipped`; the run must succeed. (`05-rerun.txt`)
2. **Interrupt and resume:** start a third apply and press Ctrl-C during the longest step
   (the deploy). Expect exit 130 and status `interrupted`; `doogle resume latest
   --approve-high-risk` must finish it. (`06-interrupt-resume.txt`)
3. **Teardown only removes what this run created:** for the re-run from check 1, run
   `doogle teardown <that-run-id>`: entries whose steps were skipped must show `WILL SKIP`.
   Do **not** apply that teardown. (`07-teardown-preview-rerun.txt`)

## 5. Tear down

```bash
doogle teardown <first-run-id>                                   | tee ~/evidence/$BP/08-teardown-plan.txt
doogle teardown <first-run-id> --apply --approve-high-risk 2>&1 | tee ~/evidence/$BP/09-teardown.txt
```

Do every `MANUAL` teardown entry it prints (for Cloud Run: delete the `cloud-run-source-deploy`
Artifact Registry images and the `run-sources-$P-<region>` bucket contents if nothing else uses
them). Record what you did by hand in `10-manual-teardown.txt`.

## 6. Zero-leftover check

```bash
sleep 120   # deletions take a moment to reach the asset index
gcloud asset search-all-resources --scope="projects/$P" \
  --format="value(assetType,name)" | sort > evidence/$BP/assets-after.txt
gcloud projects get-iam-policy "$P" --format=json > evidence/$BP/project-iam-after.json
gcloud services list --enabled --project "$P" --format="value(config.name)" | sort > evidence/$BP/services-after.txt

diff evidence/$BP/assets-before.txt evidence/$BP/assets-after.txt  > evidence/$BP/assets.diff
diff <(jq -S . evidence/$BP/project-iam-before.json) <(jq -S . evidence/$BP/project-iam-after.json) \
  > evidence/$BP/iam.diff
diff evidence/$BP/services-before.txt evidence/$BP/services-after.txt > evidence/$BP/services.diff
```

Pass criteria:

* `assets.diff` is empty, **or** every remaining line is on the blueprint's documented
  manual-teardown list (for example the Artifact Registry repository, the `run-sources-*` bucket,
  the Firebase-managed default Hosting site, which Firebase does not allow deleting). Anything
  else is a teardown bug: file it with the drift template.
* `iam.diff` is empty (bindings Doogle added are gone; bindings that existed before are intact).
* `services.diff` only shows APIs the blueprint enabled. Doogle deliberately does not disable APIs
  (other things may depend on them); they cost nothing while unused and go away with the project.

## 7. Destroy the project and the budget

```bash
gcloud projects delete "$P" --quiet                 # 30-day soft delete; billing stops now
gcloud billing projects unlink "$P" 2>/dev/null || true
gcloud billing budgets delete "$(cat budget.name)" --billing-account="$BA" --quiet
gcloud projects describe "$P" --format="value(lifecycleState)"   # expect DELETE_REQUESTED
# Apps Script: trash the test spreadsheet and script in Drive (workspace-sheet-webhook).
```

A week later, check the billing report filtered to the project and add the total to the evidence.

## 8. Evidence bundle and the upgrade to `live`

```bash
cd evidence/$BP
# Redact identifiers before sharing.
sed -i -e "s/$P/PROJECT_ID/g" -e "s/$BA/BILLING_ACCOUNT/g" \
  -e 's/[A-Za-z0-9._%+-]\+@gmail\.com/EMAIL/g' -e 's#https://[a-z0-9-]\+\.run\.app#https://SERVICE_URL#g' *
grep -rIl "$P\|@gmail\.com" . && echo "STILL CONTAINS IDENTIFIERS: fix before sharing"
```

Open a PR that changes only the blueprint's metadata:

```yaml
last_verified: "YYYY-MM-DD"               # the day of the run
verification_status: live
verified_versions: {gcloud: "...", firebase-tools: "...", python: "..."}   # what you recorded in step 0
verification_notes: >-
  Run end to end on a disposable project on YYYY-MM-DD by @you following docs/LIVE_TEST_PLAN.md:
  apply, verify, smoke test, re-run (idempotent), interrupt + resume, teardown; zero-leftover
  check passed except the documented manual items. Cost: <cents>.
```

Attach the redacted evidence files (01-10, the three `.diff` files, the cost) to the PR. A
maintainer who did not do the run reviews it. Required to merge:

- [ ] `02-apply.txt` ends with `SUCCEEDED`, every verify `passed`
- [ ] `04-smoke.txt` shows the deployed thing answering
- [ ] checks 4.1-4.3 behaved as stated
- [ ] `09-teardown.txt` shows `SUCCEEDED` (or `NOTHING_TO_RUN` entries with reasons)
- [ ] the three diffs meet the pass criteria
- [ ] the project shows `DELETE_REQUESTED` and the budget was removed
- [ ] no unredacted identifiers anywhere

Any failure is still valuable: fix the blueprint (or file it), keep `syntax-checked`, and rerun.
