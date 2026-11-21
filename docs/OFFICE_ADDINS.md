# aiDocuMines Office add-ins (Outlook and Word)

The add-ins are static pages served by the frontend at `https://aidocumines.com/addins/`. They sign in as the
person (the same OAuth flow with PKCE as the web app), so every action follows that person's organisation, role,
matter access, legal holds, check-outs and ethical walls.

| Add-in | What it does | Manifest |
|---|---|---|
| Outlook | Suggests the matter (reference or name in the subject, where the sender's emails were filed before), files the email (.eml) and its attachments into the matter through the shared ingestion pipeline, searches documents | `/addins/manifest-outlook.xml` |
| Word | Opens Word documents from aiDocuMines, saves as a new version (previous bytes kept as a version) or as a new document in a matter, inserts clauses from the clause library, reviews the document against a playbook | `/addins/manifest-word.xml` |

## Deploy to your organisation (Microsoft 365 administrator)

1. Microsoft 365 admin centre, **Settings, Integrated apps, Upload custom apps**.
2. Choose **Provide link to manifest file** and enter
   `https://aidocumines.com/addins/manifest-outlook.xml` (then repeat for `manifest-word.xml`).
3. Assign to the people or groups who should have it. It appears in Outlook (a **File to matter** button when
   reading a message) and in Word (an **aiDocuMines** button on the Home tab) within a few hours.

For testing, sideload instead: Outlook, **Get add-ins, My add-ins, Add a custom add-in, Add from URL**; Word,
**Insert, Add-ins, Upload my add-in** with the manifest file.

## Configuration

* `public/addins/config.js` holds the API address (`window.AIDOC_API_URL`). Change it per environment; no rebuild.
* If the site is not `https://aidocumines.com`, replace that address in both manifests (every URL must be HTTPS
  and on the same domain listed in `AppDomains`).
* Organisations that require single sign-on: people sign in to aiDocuMines in the browser; the add-in sign-in
  dialog takes a password plus emailed code (administrators' break-glass route) only.

## API

`/api/v1/addins/outlook/suggest/`, `/api/v1/addins/outlook/file/`, `/api/v1/addins/word/save/`,
`/api/v1/addins/word/clauses/` (see `addins/views.py`). Limits: 50 MB per file and 150 MB per filed email,
50 attachments, 100 MB per Word save.
