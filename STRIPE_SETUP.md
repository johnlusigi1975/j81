# J81 — Paid access setup (Stripe → 90-day membership)

Honest model: customers pay **$100 for 90 days of access to the tools** — not a
promise of profit. The paywall shows a risk disclaimer (synthetics are an
audited RNG with a house edge; no tool predicts the next tick).

Flow: **Connect Deriv → pick Demo/Real → pay $100 → desk unlocks for 90 days.**

---

## 1. Pick your secrets

- **ADMIN_KEY** — any long random string (protects your `/owner` console). Make one:
  ```bash
  python3 -c "import secrets; print('j81_' + secrets.token_urlsafe(32))"
  ```
  Copy the output; you'll paste it into Render (never share it).

## 2. Stripe (test mode first)

1. Create a **Product** → "J81 Trade Desk — 90-day access", price **$100 USD**, one-time.
2. Create a **Payment Link** for it. In the link's settings:
   - **After payment → Redirect** to:
     `https://j81-trade-desk.onrender.com/?paid={CHECKOUT_SESSION_ID}`
   - Copy the **Payment Link URL** → this is `ACCESS_BUY_URL`.
3. **Developers → Webhooks → Add endpoint**:
   - URL: `https://j81-trade-desk.onrender.com/webhooks/stripe`
   - Event: **`checkout.session.completed`**
   - Save → copy the **Signing secret** (`whsec_…`) → this is `STRIPE_WEBHOOK_SECRET`.

## 3. Render → j81-trade-desk → Environment

| Key | Value |
|---|---|
| `ADMIN_KEY` | (the secret from step 1) |
| `ACCESS_BUY_URL` | (your Stripe Payment Link URL) |
| `STRIPE_WEBHOOK_SECRET` | `whsec_…` |
| `ACCESS_PRICE_LABEL` | `$100` (optional) |
| `ACCESS_DAYS` | `90` (optional) |
| `REQUIRE_ACCESS` | `false` for now → `true` when ready to charge |

Save → service redeploys.

## 4. Test before charging

1. With `REQUIRE_ACCESS=false`: open `https://j81-trade-desk.onrender.com/owner`,
   enter `ADMIN_KEY`, **mint 1 code**, and redeem it on the site to confirm unlock.
2. Set `REQUIRE_ACCESS=true`. In Stripe **test mode**, buy with test card
   `4242 4242 4242 4242` (any future date / any CVC). You should be redirected
   back and **auto-unlocked** within a few seconds.
3. Switch Stripe to **live mode** (live Payment Link + live webhook secret) and
   you're selling.

## How code delivery works

- **Automatic:** Stripe webhook mints a code on payment; the buyer's return page
  (`/?paid=…`) fetches + redeems it automatically. Nothing to email.
- **Manual fallback:** mint codes anytime at `/owner` and send them out; customers
  redeem on the paywall ("Already paid? Enter your access code").

## Endpoints (reference)

- `GET /access/status` — membership state + offer
- `POST /access/redeem` `{code}` — redeem a code (binds to the browser session)
- `POST /webhooks/stripe` — Stripe → mints a code (needs `STRIPE_WEBHOOK_SECRET`)
- `GET /access/code?session_id=…` — buyer success page fetches the issued code
- `POST /admin/licenses` `{count,note}` + `GET /admin/licenses` — owner only (`X-Admin-Key`)
