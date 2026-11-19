# Solo Serverless API on Cloud Run

Deploy a small HTTP API straight from source to Cloud Run. Cloud Build packages your code
(using Google Cloud buildpacks, or your `Dockerfile` if present), Artifact Registry stores the
image, and Cloud Run serves it on an HTTPS URL that scales to zero when idle.

## Is this the right blueprint?

| Situation | Better choice |
|---|---|
| Static site or SPA only | Firebase Hosting |
| A few triggers on Firestore/Auth events | Cloud Functions for Firebase |
| Low-traffic API, any language, public URL | **This blueprint** |
| Always-hot, latency-critical, stateful | Cloud Run with min instances > 0, GKE Autopilot, or a VM |

## Before you start

1. A Google Cloud project with billing linked (`gcloud billing projects describe PROJECT_ID`).
2. `gcloud auth login`, then `doogle doctor`.
3. An app that listens on the port in the `PORT` environment variable (Cloud Run sets it, usually 8080).

Minimal Python example (`main.py` + `requirements.txt` in the same folder):

```python
# main.py
import os
from flask import Flask, jsonify

app = Flask(__name__)

@app.get("/")
def hello():
    return jsonify(status="ok", service=os.environ.get("K_SERVICE", "local"))

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
```

```text
# requirements.txt
flask
gunicorn
```

Python buildpacks start the app with gunicorn when it is listed; otherwise add a `Procfile`
(`web: gunicorn -b :$PORT main:app`) or a `Dockerfile`.

## Run it

```bash
doogle plan solo-serverless-api --var project_id=YOUR_PROJECT_ID          # dry run, nothing changes
doogle plan solo-serverless-api --var project_id=YOUR_PROJECT_ID \
  --apply --approve-high-risk                                             # asks you to type the blueprint id
```

`deploy-service` is marked **high risk and public** because `--allow-unauthenticated` makes the
URL reachable by anyone on the internet. The verify step checks that the service reports an
`https://` URL; then try it:

```bash
curl "$(gcloud run services describe doogle-api --region us-central1 --project YOUR_PROJECT_ID --format='value(status.url)')"
```

## Keep it private instead

If only your own systems call the API, deploy without public access and call it with an identity token:

```bash
gcloud run deploy doogle-api --source . --region us-central1 --project YOUR_PROJECT_ID --no-allow-unauthenticated
curl -H "Authorization: Bearer $(gcloud auth print-identity-token)" https://YOUR-SERVICE-URL
```

## Cost guard-rails

* `--max-instances 3` caps runaway scaling; raise it deliberately.
* Create a budget alert in Billing > Budgets & alerts before sharing the URL.
* Free tiers apply per billing account, so check the official pricing pages rather than assuming.

## Troubleshooting

| Symptom | Fix |
|---|---|
| Build fails with `PERMISSION_DENIED` / `does not have permission` for the `...-compute@developer.gserviceaccount.com` account | Source builds run as the Compute Engine default service account. Grant it the builder role (find PROJECT_NUMBER with `gcloud projects describe PROJECT_ID --format='value(projectNumber)'`): `gcloud projects add-iam-policy-binding PROJECT_ID --member=serviceAccount:PROJECT_NUMBER-compute@developer.gserviceaccount.com --role=roles/run.builder`, wait two minutes, then `doogle resume latest`. See https://cloud.google.com/run/docs/deploying-source-code#required_roles |
| Storage keeps growing after teardown | Source archives live in the bucket `run-sources-PROJECT_ID-REGION` and images in Artifact Registry `cloud-run-source-deploy`. They are shared by all source deploys in the region, so Doogle does not delete them; remove old ones in the console. |
| `PERMISSION_DENIED` on enable-apis | You need `roles/serviceusage.serviceUsageAdmin` or Owner on the project. |
| Build fails: "no buildpack groups passed detection" | Add a `Dockerfile` or a recognised manifest (`requirements.txt`, `package.json`, `go.mod`...). |
| Container failed to start / listen on port | Your app must bind `0.0.0.0:$PORT`. Check `gcloud run services logs read SERVICE --region REGION`. |
| Billing not enabled | Link a billing account to the project in the console. |

Then resume where it stopped: `doogle resume latest`.

## Teardown

```bash
doogle teardown latest                                  # shows what will be deleted
doogle teardown latest --apply --approve-high-risk      # deletes the service
```

Images in the `cloud-run-source-deploy` Artifact Registry repository are shared by every source
deploy in the region, so Doogle leaves them for you to review manually.
