# Engine timer

The engine (`.github/workflows/signal-check.yml`) is scheduled `*/15 * * * *`,
but GitHub does not fire frequent schedules reliably. Measured on
2026-10-19: 22 scheduled runs in 3 days against 288 intended (8%), a median
gap of 199 minutes. No run failed; GitHub simply skipped them.

A run started through the API (`workflow_dispatch`) starts within seconds,
so an outside timer calls that every 15 minutes. GitHub's own schedule
stays in the workflow as a backup: if the timer stops, the engine still
runs a few times a day instead of not at all.

## 1. GitHub token

github.com → Settings → Developer settings → Personal access tokens →
**Fine-grained tokens** → Generate new token

| Setting | Value |
|---|---|
| Resource owner | `xenovasoft` |
| Repository access | Only select repositories → `trading-app` |
| Repository permissions → **Actions** | **Read and write** |
| Everything else | No access (Metadata: read-only is added automatically) |
| Expiration | Up to 1 year. Note the date: the timer stops working when it expires |

With only this permission the token can start and cancel workflow runs on
this one repository. It cannot read secrets, push code, or touch any other
repository.

## 2. Timer at cron-job.org

Create a free account, then **Create cronjob**:

| Field | Value |
|---|---|
| URL | `https://api.github.com/repos/xenovasoft/trading-app/actions/workflows/signal-check.yml/dispatches` |
| Schedule | Every 15 minutes |
| Request method (Advanced) | `POST` |
| Request body | `{"ref":"main"}` |

Headers (Advanced → Headers):

| Header | Value |
|---|---|
| `Authorization` | `Bearer <your token>` |
| `Accept` | `application/vnd.github+json` |
| `X-GitHub-Api-Version` | `2022-11-28` |
| `User-Agent` | `signal-engine-timer` |

A successful call returns **HTTP 204** with an empty body. This exact
request was verified on 2026-10-19 and started a run within seconds.

## 3. Check it works

Open `/terminal.html` on the live demo. The status line reads
"last checked … (N minutes ago)". Within 15 minutes of turning the timer on
it should stay under 15 minutes and lose the amber "overdue" warning.

From a terminal:

```bash
gh run list --workflow=signal-check.yml --limit 6
```

Runs should be about 15 minutes apart, with event `workflow_dispatch`.

## If it stops

- **HTTP 401:** the token expired or was revoked. Make a new one (step 1)
  and paste it into the timer's `Authorization` header.
- **HTTP 404:** the URL is wrong, or the token can't see this repository.
- **HTTP 422:** the body is missing or the branch name is wrong; it must be
  `{"ref":"main"}`.
