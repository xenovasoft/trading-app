# User accounts

Each user logs in and gets their own demo trading account and a demo wallet.
Nobody else can start, stop or reset it. The account is stored on the
server, so it looks the same on every page, tab and device, and in the iOS app.

- **Wallet:** add demo money instantly with no payment details, or check
  out with the test card `4242 4242 4242 4242`. Payouts go to a demo bank
  account. All of these happen immediately.
- **Start trading:** moves money from the wallet into a trading account.
  The engine opens it on its next cycle, within 15 minutes.
- **What it trades:** the active demo strategy in `demotrader.py`, on live
  5-minute candles for BTC, gold and silver. It buys or sells when the 9-
  and 21-candle averages cross. The stop is 1.5x ATR, never closer than 3x
  costs, and the target is 2x the stop. An order closes after 4 hours if
  neither is hit. It's for testing the product, not the signal engine; the
  shared account still trades only real setups.
- **Never stops by itself:** no drawdown, trade-count or age limit. It runs
  until the user presses Stop.
- **Stop:** on its next cycle the engine closes every open order at the
  market price and puts the balance back in the wallet. A start that
  hasn't been picked up yet can be cancelled, and the money comes back
  immediately.

The shared demo account is still there as "Shared demo (everyone)".

How it's built is explained at the top of `accounts.py`.

## Turning it on in production

1. **Create the tables.** Supabase → SQL editor → paste
   `user_accounts.sql` → Run.
2. **Add the session secret.** Vercel → project → Settings → Environment
   Variables → add `AUTH_SECRET`, set to a random string of 32 or more
   characters. One way to make one:

   ```bash
   python3 -c "import secrets; print(secrets.token_urlsafe(32))"
   ```

   `SUPABASE_SECRET_KEY` must also be set. The shared account's Stop
   button already needs it.
3. **Redeploy** so the new variable takes effect: push to `main`, or press
   Redeploy in Vercel.
4. **Make logins.** Either use "Create account" on `/login.html`, or create
   the test users from `test_users.local.json` (gitignored):

   ```bash
   SUPABASE_URL=… SUPABASE_SECRET_KEY=… python accounts.py seed --remote
   ```

Before step 1 the engine prints `user accounts unavailable` and keeps
running the shared account. Before step 2 the login page shows
"AUTH_SECRET not configured".

## Locally

```bash
python devserver.py
```

The server creates every user listed in `test_users.local.json` in
`accounts.local.json` (gitignored). Then log in at
`http://localhost:8934/login.html`. Locally, Start and Stop take effect
immediately, and the server itself steps every running account on live
candles once a minute.

In production, user accounts move only when the engine runs. GitHub's own
schedule fires a few times a day, so set up the outside timer in
`docs/engine-timer.md` for a check every 15 minutes. Late runs still put
every order at the right candle, because each run replays the candles it
missed (up to about 60 hours of them).

To add another local user:

```bash
python accounts.py add NAME
```
