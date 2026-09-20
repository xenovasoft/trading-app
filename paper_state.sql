-- Storage for the paper-trading forward test (papertrader.py + /api/paper).
--
-- One row, id = 1. `payload` is papertrader.snapshot() written by the engine
-- at the end of each cycle; `requested` is the website's pending arm/disarm
-- intent, which the next engine cycle consumes and clears.
--
-- Split that way on purpose: the engine is the only writer of truth, and the
-- website can only ask. A control path where the browser could write session
-- state directly would let a stale tab resurrect a session the engine had
-- already auto-stopped.

create table if not exists public.paper_state (
  id          integer primary key,
  payload     jsonb,
  requested   text check (requested in ('arm','disarm')),
  updated_at  timestamptz default now()
);

alter table public.paper_state enable row level security;

-- The website reads with the publishable key, so anon needs SELECT and
-- nothing else. Writes go through the service key in /api/paper-control,
-- which bypasses RLS.
drop policy if exists paper_state_read on public.paper_state;
create policy paper_state_read on public.paper_state
  for select using (true);

insert into public.paper_state (id, payload, requested)
values (1, '{"status":"DISARMED"}'::jsonb, null)
on conflict (id) do nothing;
