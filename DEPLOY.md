# Deploy J81 — get a public URL

This gives J81 Trade Desk a permanent, always-on URL (e.g. `https://j81-trade-desk.onrender.com`)
that anyone can open, and that you can register on Deriv.

Only the **Bot** is the public/client URL. It calls the **Analyser** behind the
scenes; the **Researcher** (optional) feeds the Analyser strategies.

---

## 1. Put the code on GitHub
From the project folder:

```bash
git add -A
git commit -m "J81: deploy config (Dockerfiles + render.yaml)"
gh repo create j81 --private --source=. --push      # or create the repo on github.com and push
```

(If you're not using the `gh` CLI: create an empty private repo on github.com,
then `git remote add origin <url>` and `git push -u origin master`.)

## 2. Create the services on Render
1. Go to **render.com** → sign up / log in.
2. **New +** → **Blueprint** → connect GitHub → pick the **j81** repo.
3. Render reads `render.yaml` and shows 3 services: **j81-trade-desk, j81-analyser,
   j81-researcher**. Click **Apply**.
   - Want to start cheaper? Delete/skip **j81-researcher** for now (the trading
     app + scanner work on just **bot + analyser**). You can add it later.

> 24/7 + the data disks need the **starter** plan (~$7/mo per service). The free
> tier sleeps and can't keep a disk, so accounts/trades wouldn't persist.

## 3. Set the secrets (Render dashboard → each service → Environment)
**j81-trade-desk**
- `BOT_ENCRYPTION_KEY` → generate one and paste it. **Keep it forever** (changing
  it makes stored account tokens unreadable). Generate locally with:
  ```bash
  python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
  ```
- `DERIV_APP_ID` → your Deriv app id (after step 5). Leave blank to start.
- `DERIV_OAUTH_TOKEN_URL` → only for the NEW-platform OAuth2 (PKCE); leave blank for legacy.
- `DRY_RUN` is `true` by default — keep it until you've tested on a demo account.

**j81-researcher** (if deployed)
- `GOOGLE_API_KEY` → your free Gemini key (aistudio.google.com/apikey).

## 4. Confirm the URLs match
After the first deploy, open each service and check its URL. If Render appended a
suffix (e.g. `j81-analyser-ab12.onrender.com`), update these to the real URLs:
- **j81-trade-desk** → `ANALYSER_URL`, `DERIV_OAUTH_REDIRECT_URI`
- **j81-analyser** → `RESEARCHER_URL`
- **j81-researcher** → `LOGGING_APP_URL`, `COMMS_HUB_URL`

Your public app is now **`https://j81-trade-desk.onrender.com`**. Open it — the connect
screen (with the gold-bar background) should load, and **Preview the app (demo)**
works immediately.

## 5. Register the app on Deriv (to go live + earn markup)
1. **api.deriv.com/dashboard** → register an app.
2. Website / redirect URL: **`https://j81-trade-desk.onrender.com/oauth/callback`**
   (now allowed — it's a real public URL, not localhost).
3. Set your markup %.
4. Put the app id in **`DERIV_APP_ID`** on j81-trade-desk. OAuth ("Connect with Deriv")
   now works for your users.

## 6. Going live with real money (do last, carefully)
- Test on a **demo** account first with `DRY_RUN=true` (practice), then a tiny
  real stake with `DRY_RUN=false`.
- Keep per-account stake/daily limits and take-profit on.

---

### Notes
- A public URL means anyone with the link can open it and connect **their own**
  Deriv account — that's the intended model. Consider setting `INCOMING_API_KEY`
  on the Analyser and reviewing the admin endpoints before sharing widely.
- Railway/Fly work too — same Dockerfiles; set the same env vars + a volume at
  `/var/data`, and bind to `$PORT`.
