# User accounts

Each user logs in and gets their own demo trading account and a demo wallet.
Nobody else can start, stop or reset it. The account is stored on the
server, so it looks the same on every page, tab and device, and in the iOS app.

- **Wallet:** add demo money instantly with no payment details, or check
  out with the test card `4242 4242 4242 4242`. Payouts go to a demo bank
  account. All of these happen immediately.
- **Start trading:** moves money from the wallet into a trading account.
  The engine opens it on its next cycle, within 15 minutes.
- **Stop:** the engine closes the account on its next cycle and puts its
  balance back in the wallet. A start that hasn't been picked up yet can be
  cancelled, and the money comes back immediately.

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
immediately, the same as the shared account's buttons do on this server.

To add another local user:

```bash
python accounts.py add NAME
```
