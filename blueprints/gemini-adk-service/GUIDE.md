# Gemini ADK Agent Service on Cloud Run + Vertex AI

Host an [Agent Development Kit](https://google.github.io/adk-docs/) agent as a **private**
Cloud Run service. The agent calls Gemini through Vertex AI using a dedicated service account
that holds only `roles/aiplatform.user`, so there are no API keys in your code or container.

## Is this the right blueprint?

| Situation | Better choice |
|---|---|
| One prompt in, one answer out | Call Gemini directly with the `google-genai` SDK |
| Still iterating on prompts | Google AI Studio, then "Get code" |
| Want managed sessions, memory, scaling for agents | Vertex AI Agent Engine (`adk deploy agent_engine`) |
| Custom agent + tools, served as your own API | **This blueprint** |

## 1. Create the agent folder

```text
my-agent/
├── helper_agent/
│   ├── __init__.py
│   └── agent.py
├── main.py
├── requirements.txt
└── Dockerfile
```

```python
# helper_agent/__init__.py
from . import agent
```

```python
# helper_agent/agent.py
import datetime
from google.adk.agents import Agent


def get_utc_time() -> dict:
    """Returns the current UTC time."""
    return {"utc": datetime.datetime.now(datetime.timezone.utc).isoformat()}


root_agent = Agent(
    name="helper_agent",
    model="gemini-2.5-flash",  # pick any Gemini model available in your Vertex AI location
    description="A small helper agent.",
    instruction="Answer briefly. Use get_utc_time when asked about the time.",
    tools=[get_utc_time],
)
```

```python
# main.py
import os

import uvicorn
from google.adk.cli.fast_api import get_fast_api_app

AGENTS_DIR = os.path.dirname(os.path.abspath(__file__))
app = get_fast_api_app(agents_dir=AGENTS_DIR, web=False)

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
```

```text
# requirements.txt
google-adk
```

```dockerfile
# Dockerfile
FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
RUN adduser --disabled-password --gecos "" appuser && chown -R appuser /app
USER appuser
CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port ${PORT:-8080}"]
```

Test locally first: `pip install google-adk`, `gcloud auth application-default login`, export
`GOOGLE_GENAI_USE_VERTEXAI=TRUE GOOGLE_CLOUD_PROJECT=YOUR_PROJECT_ID GOOGLE_CLOUD_LOCATION=us-central1`,
then run `adk web` from `my-agent/`.

## 2. Plan, then apply

```bash
cd my-agent
doogle plan gemini-adk-service --var project_id=YOUR_PROJECT_ID
doogle plan gemini-adk-service --var project_id=YOUR_PROJECT_ID --apply --approve-high-risk
```

`grant-vertex-user` is **high risk** because it changes project IAM. Review the member and role
in the plan before approving. The deploy uses `--no-allow-unauthenticated`, so the service is not
public.

## 3. Call the agent

```bash
URL="$(gcloud run services describe doogle-agent --region us-central1 --project YOUR_PROJECT_ID --format='value(status.url)')"
TOKEN="$(gcloud auth print-identity-token)"
curl -H "Authorization: Bearer $TOKEN" "$URL/list-apps"
curl -X POST -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  "$URL/apps/helper_agent/users/u1/sessions/s1" -d '{}'
curl -X POST -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" "$URL/run" \
  -d '{"app_name":"helper_agent","user_id":"u1","session_id":"s1","new_message":{"role":"user","parts":[{"text":"What time is it?"}]}}'
```

Or open a local authenticated tunnel: `gcloud run services proxy doogle-agent --region us-central1 --project YOUR_PROJECT_ID`.

Sessions above are in memory and are lost when an instance scales down. For durable sessions,
pass a `session_service_uri` (for example a Cloud SQL database) to `get_fast_api_app`, or use
Agent Engine.

## Alternative: ADK's own deploy command

`adk deploy cloud_run --project YOUR_PROJECT_ID --region us-central1 ./helper_agent` builds and
deploys for you. Doogle uses explicit `gcloud` steps instead so every resource (service account,
IAM binding, env vars, privacy setting) is visible in the plan and recorded for teardown.

## Cost guard-rails

* Gemini tokens are the main cost; agents that loop or call tools multiply them. Log token usage
  and set a budget alert.
* `--max-instances 2` caps concurrency spend; raise it deliberately.

## Troubleshooting

| Symptom | Fix |
|---|---|
| Build fails with `PERMISSION_DENIED` / `does not have permission` for the `...-compute@developer.gserviceaccount.com` account | Source builds run as the Compute Engine default service account. Grant it the builder role (find PROJECT_NUMBER with `gcloud projects describe PROJECT_ID --format='value(projectNumber)'`): `gcloud projects add-iam-policy-binding PROJECT_ID --member=serviceAccount:PROJECT_NUMBER-compute@developer.gserviceaccount.com --role=roles/run.builder`, wait two minutes, then `doogle resume latest`. See https://cloud.google.com/run/docs/deploying-source-code#required_roles |
| Storage keeps growing after teardown | Source archives live in the bucket `run-sources-PROJECT_ID-REGION` and images in Artifact Registry `cloud-run-source-deploy`. They are shared by all source deploys in the region, so Doogle does not delete them; remove old ones in the console. |
| `403 Permission 'aiplatform.endpoints.predict' denied` | The binding has not propagated yet (wait a minute) or the service runs as a different account. |
| Build fails with permission errors on a new project | Cloud Build's service account may need `roles/run.builder` / `roles/artifactregistry.writer`; see the Cloud Run source-deploy docs. |
| Model not found | The model name in `agent.py` is not available in `vertex_location`; try `global` or another region. |
| `check-agent-source` fails | Run doogle from the agent folder or pass `--var agent_dir=path/to/my-agent`. |

Resume after a fix: `doogle resume latest --approve-high-risk`.

## Teardown

```bash
doogle teardown latest
doogle teardown latest --apply --approve-high-risk
```

This deletes the service, removes the IAM binding and deletes the service account. Images in
Artifact Registry and enabled APIs are left for you to review.
