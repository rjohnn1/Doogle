# Graduate a Gemini Prototype from AI Studio to Vertex AI

Your prototype calls Gemini with an API key from Google AI Studio. This blueprint moves it to
**Vertex AI** in your Google Cloud project: same models, but authenticated with IAM
(Application Default Credentials locally, a least-privilege service account in production), with
Cloud billing, quotas, regional control and audit logs. If your code uses the `google-genai`
SDK, the switch is configuration, not a rewrite.

## What is automated and what is not

| Step | Automated? | Why |
|---|---|---|
| Enable Vertex AI API | Yes | `gcloud services enable` |
| Create runtime service account | Yes (skipped if it exists) | `gcloud iam service-accounts create` |
| Grant `roles/aiplatform.user` | Yes, **high risk** (IAM) | `add-iam-policy-binding` |
| Local ADC login | **Manual** | Opens a browser for your own Google account |
| Change app env / client options | **Manual** | It is your code and your deploy config |
| Run the app as the service account | **Manual** | Depends on where you deploy |
| Retire the AI Studio key | **Manual** | Do it only after Vertex AI works |

Doogle is honest about this: the automated part makes the project ready; you still flip the app.

## Is this the right blueprint?

| Situation | Better choice |
|---|---|
| Hobby project on the free tier | Stay on AI Studio / Gemini API |
| Need an agent server with tools | `gemini-adk-service` (includes this setup + Cloud Run) |
| Production app needing IAM, governance, Cloud billing | **This blueprint** |

## Run it

```bash
doogle plan ai-studio-to-vertex --var project_id=YOUR_PROJECT_ID
doogle plan ai-studio-to-vertex --var project_id=YOUR_PROJECT_ID --apply --approve-high-risk
```

The run pauses at each manual step. When you have done it, `doogle resume latest`.

## Manual step: local ADC

```bash
gcloud auth application-default login
gcloud auth application-default set-quota-project YOUR_PROJECT_ID
```

## Manual step: switch the app

The `google-genai` SDK picks Vertex AI from environment variables, so this is often the only change:

```bash
unset GEMINI_API_KEY GOOGLE_API_KEY
export GOOGLE_GENAI_USE_VERTEXAI=True
export GOOGLE_CLOUD_PROJECT=YOUR_PROJECT_ID
export GOOGLE_CLOUD_LOCATION=global        # or a region such as us-central1
```

Or explicitly in code (Python):

```python
from google import genai

# Before (AI Studio): client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
client = genai.Client(vertexai=True, project="YOUR_PROJECT_ID", location="global")
resp = client.models.generate_content(model="gemini-4-hydrogen", contents="Say hello in five words.")
print(resp.text)
```

Smoke test from your terminal (uses ADC; `pip install google-genai` first):

```bash
python -c "from google import genai; c=genai.Client(); print(c.models.generate_content(model='gemini-4-hydrogen', contents='ping').text)"
```

The examples use `gemini-4-hydrogen`, the current general-purpose generally-available Gemini 4
model on both the [Gemini API](https://ai.google.dev/gemini-api/docs/models) and
[Vertex AI](https://docs.cloud.google.com/vertex-ai/generative-ai/docs/models/gemini/4-hydrogen).
Names and availability change, so pick the latest stable (non-preview) ID from those official
pages, and confirm it is offered in your Vertex AI location. If your code still uses the older `google-generativeai`
package, migrate to `google-genai` first (see the official migration guide linked in the
blueprint's docs).

## Manual step: run as the service account

Attach the service account wherever the app runs, for example:

```bash
gcloud run deploy my-app --source . --region us-central1 --project YOUR_PROJECT_ID \
  --service-account gemini-app-runtime@YOUR_PROJECT_ID.iam.gserviceaccount.com \
  --set-env-vars GOOGLE_GENAI_USE_VERTEXAI=True,GOOGLE_CLOUD_PROJECT=YOUR_PROJECT_ID,GOOGLE_CLOUD_LOCATION=global
```

**Never download a service-account key file** for this. Outside Google Cloud, use Workload
Identity Federation.

## Manual step: retire the API key

Only once production traffic works on Vertex AI: delete or restrict the key in AI Studio's API
keys page, or with `gcloud services api-keys list --project PROJECT_THAT_OWNS_THE_KEY` and
`gcloud services api-keys delete KEY_ID --project PROJECT_THAT_OWNS_THE_KEY`. Remove it from
`.env` files, secret managers and CI variables.

## Cost guard-rails

* There is no free model quota on Vertex AI; set a budget alert before switching traffic.
* Compare per-token prices for your model on both pricing pages; they are not always identical.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `403 ... aiplatform.endpoints.predict` | Your user (local) or the service account (deployed) lacks `roles/aiplatform.user`, or IAM has not propagated yet (wait a minute). |
| `DefaultCredentialsError` | Run the ADC login step, or attach the service account in production. |
| `404 model not found` | The model is not available in `GOOGLE_CLOUD_LOCATION`; try `global` or another region. |
| Still billed on AI Studio | An old env var or code path still uses the API key; search for `api_key=`. |

## Teardown

```bash
doogle teardown latest
doogle teardown latest --apply --approve-high-risk
```

This removes the IAM binding and deletes the service account; anything still running as it loses
access. ADC revocation and app config are manual. The API stays enabled.
