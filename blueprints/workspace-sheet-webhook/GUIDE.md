# Google Sheet + Apps Script Webhook

Create a new Google Sheet with a bound Apps Script project, push a `doPost` handler from your
terminal with [clasp](https://github.com/google/clasp), and deploy it as a web app URL that
writes every incoming webhook payload as a row.

## Is this the right blueprint?

| Situation | Better choice |
|---|---|
| Collecting answers from people | Google Forms (writes to a Sheet with zero code) |
| High volume, strict latency, many concurrent writes | Cloud Run + Firestore or BigQuery |
| Low-volume webhooks into a Sheet your team already uses | **This blueprint** |

## Before you start

```bash
npm install -g @google/clasp
clasp login
```

Turn on the Apps Script API for your account at <https://script.google.com/home/usersettings>.
Then work in an empty folder: `mkdir sheet-webhook && cd sheet-webhook`.

## Run it

```bash
doogle plan workspace-sheet-webhook                       # dry run with default title "Doogle Webhook Inbox"
doogle plan workspace-sheet-webhook --apply --approve-high-risk
```

The run **pauses** at the manual step `add-webhook-code`. Create the two files below in
`src/` (or your `root_dir`), replacing what clasp generated, then continue with
`doogle resume latest --approve-high-risk`.

### `src/Code.js`

```javascript
const SHEET_NAME = 'Inbox';

/** Run once from the Apps Script editor to create the tab, store IDs and generate a token. */
function setup() {
  const ss = SpreadsheetApp.getActiveSpreadsheet();
  const sheet = ss.getSheetByName(SHEET_NAME) || ss.insertSheet(SHEET_NAME);
  if (sheet.getLastRow() === 0) sheet.appendRow(['received_at', 'source', 'payload']);
  const props = PropertiesService.getScriptProperties();
  props.setProperty('SPREADSHEET_ID', ss.getId());
  if (!props.getProperty('WEBHOOK_TOKEN')) props.setProperty('WEBHOOK_TOKEN', Utilities.getUuid());
  Logger.log('Webhook token (keep it private): ' + props.getProperty('WEBHOOK_TOKEN'));
}

function doPost(e) {
  const props = PropertiesService.getScriptProperties();
  const expected = props.getProperty('WEBHOOK_TOKEN');
  const given = e && e.parameter ? e.parameter.token : null;
  if (!expected || given !== expected) return json_({ ok: false, error: 'unauthorized' });

  const lock = LockService.getScriptLock();
  lock.waitLock(10000);
  try {
    const sheet = SpreadsheetApp.openById(props.getProperty('SPREADSHEET_ID')).getSheetByName(SHEET_NAME);
    const body = e.postData ? String(e.postData.contents).slice(0, 50000) : '';
    sheet.appendRow([new Date(), safe_(e.parameter.source || ''), safe_(body)]);
  } finally {
    lock.releaseLock();
  }
  return json_({ ok: true });
}

/** Stop payloads such as "=IMPORTXML(...)" from being stored as formulas. */
function safe_(value) {
  const s = String(value);
  return /^[=+\-@]/.test(s) ? "'" + s : s;
}

function json_(obj) {
  return ContentService.createTextOutput(JSON.stringify(obj)).setMimeType(ContentService.MimeType.JSON);
}
```

### `src/appsscript.json`

```json
{
  "timeZone": "Etc/UTC",
  "dependencies": {},
  "exceptionLogging": "STACKDRIVER",
  "runtimeVersion": "V8",
  "oauthScopes": ["https://www.googleapis.com/auth/spreadsheets"],
  "webapp": {
    "executeAs": "USER_DEPLOYING",
    "access": "ANYONE_ANONYMOUS"
  }
}
```

`ANYONE_ANONYMOUS` is why `deploy-webapp` is flagged **public**: anyone with the URL can call it.
The token check is what keeps strangers out, so treat the token like a password and never commit it.

## Authorize and test (manual step `authorize-and-test`)

1. Open the Sheet, then **Extensions > Apps Script**, select `setup` and click **Run**. Grant access.
2. Copy the token from **Executions** / the log.
3. Get the deployment ID from `clasp list-deployments` (the one with your description). The URL is
   `https://script.google.com/macros/s/DEPLOYMENT_ID/exec`.
4. Send a test request (curl follows Google's redirect; omit `-X POST` so the redirect works):

```bash
curl -L -H "Content-Type: application/json" -d '{"hello":"world"}' \
  "https://script.google.com/macros/s/DEPLOYMENT_ID/exec?token=YOUR_TOKEN&source=curl"
```

A new row should appear in the **Inbox** tab and the response is `{"ok":true}`.

## Limits to know

* Web apps always answer HTTP 200; check the `ok` field instead of the status code.
* Apps Script cannot read request headers in `doPost`, so the token travels as a query parameter.
* Quotas (executions per day, runtime per execution) depend on your account type; see the official quotas page.
* After code changes, `clasp push --force` then `clasp create-deployment --deploymentId DEPLOYMENT_ID`
  to update the same URL instead of creating a new one.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `User has not enabled the Apps Script API` | Enable it at script.google.com/home/usersettings, wait a minute, `doogle resume latest`. |
| `show-clasp-account` fails | Run `clasp login`. |
| Response is an HTML sign-in page | The deployment is not `ANYONE_ANONYMOUS`; check `appsscript.json` and redeploy. |
| `{"ok":false,"error":"unauthorized"}` | Wrong or missing `token` query parameter, or `setup()` was never run. |

## Teardown

```bash
doogle teardown latest
doogle teardown latest --apply --approve-high-risk     # deletes all deployments: the URL stops working
```

Then move the spreadsheet to the Drive trash (this removes the bound script too) and delete the
local `.clasp.json` and `src/` if you no longer need them.
