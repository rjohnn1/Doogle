# Firebase Auth MVP: Hosting + Authentication + Firestore

A serverless web MVP: users sign in with Firebase Authentication, each user's data lives in
Cloud Firestore protected by security rules, and the site is served by Firebase Hosting at
`https://PROJECT_ID.web.app`. No servers to run.

## Is this the right blueprint?

| Situation | Better choice |
|---|---|
| Static site, no users | Firebase Hosting only |
| SSR / Next.js / Angular SSR | Firebase App Hosting |
| Relational data, reporting, joins | Cloud SQL + Cloud Run API |
| Secret server-side logic (payments, admin) | Add Cloud Functions or Cloud Run; never trust the browser |
| Small web app with sign-in and per-user data | **This blueprint** |

## Before you start

```bash
npm install -g firebase-tools
firebase login
gcloud auth login
mkdir my-mvp && cd my-mvp
```

## Run it

```bash
doogle plan firebase-auth-mvp --var project_id=YOUR_PROJECT_ID                       # dry run
doogle plan firebase-auth-mvp --var project_id=YOUR_PROJECT_ID --apply --approve-high-risk
```

`add-firebase`, `create-firestore` and `register-web-app` are **idempotent**: each has a
read-only `skip_if` check, so re-running on a project that already has Firebase, a database or the
app does not fail. The run pauses twice for manual steps; finish them and run
`doogle resume latest --approve-high-risk`.

> The Firestore location (`firestore_location`, default `nam5` in the US) is **permanent**. Pick
> one near your users, e.g. `eur3` for Europe or `asia-south1` for India.

### Manual step 1: enable sign-in providers

Firebase console > your project > **Build > Authentication > Get started > Sign-in method**.
Enable **Email/Password** and/or **Google**. firebase-tools has no command for this.

### Manual step 2: add the app files

Get your web config (it is public by design; security comes from the rules, not from hiding it):

```bash
firebase apps:sdkconfig WEB --project YOUR_PROJECT_ID
```

`firebase.json`

```json
{
  "hosting": {
    "public": "public",
    "ignore": ["firebase.json", "**/.*", "**/node_modules/**"]
  },
  "firestore": {
    "rules": "firestore.rules"
  }
}
```

`firestore.rules`: each signed-in user can read and write only `users/{their uid}/...`; everything
else is denied.

```text
rules_version = '2';
service cloud.firestore {
  match /databases/{database}/documents {
    match /users/{userId}/{document=**} {
      allow read, write: if request.auth != null && request.auth.uid == userId;
    }
  }
}
```

`public/index.html` (replace the `firebaseConfig` values with the output of `apps:sdkconfig`):

```html
<!doctype html>
<html lang="en">
<head><meta charset="utf-8"><title>Doogle MVP</title></head>
<body>
  <h1>Doogle MVP</h1>
  <button id="login">Sign in with Google</button>
  <button id="logout" hidden>Sign out</button>
  <p id="status">Signed out</p>
  <input id="note" placeholder="Write a private note"><button id="save" disabled>Save</button>
  <ul id="notes"></ul>
  <script type="module">
    import { initializeApp } from "https://www.gstatic.com/firebasejs/13.4.0/firebase-app.js";
    import { getAuth, GoogleAuthProvider, signInWithPopup, signOut, onAuthStateChanged }
      from "https://www.gstatic.com/firebasejs/13.4.0/firebase-auth.js";
    import { getFirestore, collection, addDoc, getDocs, serverTimestamp }
      from "https://www.gstatic.com/firebasejs/13.4.0/firebase-firestore.js";

    const firebaseConfig = {
      apiKey: "PASTE_FROM_SDKCONFIG",
      authDomain: "YOUR_PROJECT_ID.firebaseapp.com",
      projectId: "YOUR_PROJECT_ID",
      appId: "PASTE_FROM_SDKCONFIG"
    };
    const app = initializeApp(firebaseConfig);
    const auth = getAuth(app);
    const db = getFirestore(app);
    const $ = (id) => document.getElementById(id);

    $("login").onclick = () => signInWithPopup(auth, new GoogleAuthProvider());
    $("logout").onclick = () => signOut(auth);
    onAuthStateChanged(auth, async (user) => {
      $("status").textContent = user ? `Signed in as ${user.email}` : "Signed out";
      $("login").hidden = !!user; $("logout").hidden = !user; $("save").disabled = !user;
      $("notes").innerHTML = "";
      if (!user) return;
      const snap = await getDocs(collection(db, "users", user.uid, "notes"));
      snap.forEach((d) => { const li = document.createElement("li"); li.textContent = d.data().text; $("notes").append(li); });
    });
    $("save").onclick = async () => {
      const user = auth.currentUser; if (!user) return;
      await addDoc(collection(db, "users", user.uid, "notes"), { text: $("note").value, at: serverTimestamp() });
      location.reload();
    };
  </script>
</body>
</html>
```

The Firebase JS SDK version above (13.4.0) was current when this guide was written; newer
versions work the same way. A bundler setup (`npm create vite@latest`, then `npm install firebase`)
is the usual next step; set `"public": "dist"` in `firebase.json` and build before deploying.

## Verify

`deploy` is flagged **high risk and public**: anyone can open the site. Its verify step fetches
`https://PROJECT_ID.web.app/` with `curl` and expects HTML back. Then sign in, save a note, and in
the Firestore console check it is under `users/<your uid>/notes`. Try the rules in the console's
Rules Playground: an unauthenticated read of `users/someone` must be denied.

If your default Hosting site ID differs from the project ID (it can when the ID was taken), the
URL verify fails even though the deploy worked. Check `firebase hosting:sites:list --project
YOUR_PROJECT_ID` and open the URL it shows.

## Cost guard-rails

* Start on the Spark (no-cost) plan; you only need Blaze for usage beyond the free quotas or for
  some features. On Blaze, set a budget alert first.
* Firestore bills per document read and write. Avoid listening to whole collections in loops.
* Turn on App Check before launch to keep other sites from using your config.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `add-firebase` fails with permission denied | You need Owner (or Firebase Admin) on the project, and to accept the Firebase terms once in the console. |
| `create-firestore` fails: location not available | Run `firebase firestore:locations` and pick a listed one. |
| `auth/configuration-not-found` in the browser | The sign-in provider is not enabled (manual step 1). |
| `auth/unauthorized-domain` | Add your domain under Authentication > Settings > Authorized domains. |
| `Missing or insufficient permissions` | Expected for other users' data; check the path matches `users/{uid}`. |

## Teardown

```bash
doogle teardown latest
doogle teardown latest --apply --approve-high-risk
```

This stops Hosting and **permanently deletes the (default) Firestore database and all its data**.
Export it first if needed (`gcloud firestore export gs://YOUR_BUCKET --project YOUR_PROJECT_ID`).
Removing sign-in providers, test users and the web app registration is manual. Doogle never
deletes projects.
