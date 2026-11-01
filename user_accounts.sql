-- Storage for user accounts (accounts.py, /api/auth, /api/account): logins,
-- one paper trading account per user, and a demo wallet.
--
-- Run once in the Supabase SQL editor. Until it has run, the engine prints
-- "user accounts unavailable" and carries on with the shared account, and
-- the site's login answers with an error instead of breaking.
--
-- Every table has RLS on and NO policies, so the publishable key the website
-- ships can read none of it -- not even a password hash. Only the Vercel
-- endpoints and the engine touch these tables, with the secret key, which
-- bypasses RLS.

create table if not exists public.app_users (
  id             bigint generated always as identity primary key,
  username       text not null unique check (username ~ '^[a-z0-9_.-]{3,32}$'),
  -- pbkdf2_sha256$<iterations>$<salt>$<hash>, written by accounts.py or auth.js
  password_hash  text not null,
  created_at     timestamptz default now()
);

-- Same split as paper_state: `payload` is written by the engine only (the
-- snapshot plus the full state under _state); `requested` is the website's
-- pending start/stop, which the next engine cycle acts on and clears.
create table if not exists public.user_paper (
  user_id     bigint primary key references public.app_users(id) on delete cascade,
  payload     jsonb,
  requested   text check (requested = 'disarm' or requested like 'arm%'),
  updated_at  timestamptz default now()
);

-- The wallet is this ledger; its balance is sum(amount). Rows are only
-- added. `ref` makes the engine's "return the balance of a stopped account"
-- row impossible to write twice for the same session.
create table if not exists public.wallet_tx (
  id          bigint generated always as identity primary key,
  user_id     bigint not null references public.app_users(id) on delete cascade,
  kind        text not null check (kind in ('deposit', 'payout', 'allocate', 'return')),
  amount      numeric(14, 2) not null,
  method      text,
  note        text,
  ref         text unique,
  created_at  timestamptz default now()
);
create index if not exists wallet_tx_user_idx on public.wallet_tx (user_id, id desc);

alter table public.app_users  enable row level security;
alter table public.user_paper enable row level security;
alter table public.wallet_tx  enable row level security;
