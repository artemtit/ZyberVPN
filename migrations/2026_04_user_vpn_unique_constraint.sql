-- Enforce unique user_vpn rows per user in a safe, additive way
-- Run in Supabase SQL editor before deploy

-- 1) Ensure timestamp columns exist on legacy schemas
alter table if exists public.user_vpn add column if not exists created_at timestamptz not null default now();
alter table if exists public.user_vpn add column if not exists updated_at timestamptz not null default now();

-- 2) Remove duplicates, keep the newest row per user_id
with ranked as (
  select
    id,
    user_id,
    row_number() over (
      partition by user_id
      order by coalesce(updated_at, created_at) desc nulls last, id desc
    ) as rn
  from public.user_vpn
)
delete from public.user_vpn u
using ranked r
where u.id = r.id
  and r.rn > 1;

-- 3) Add unique constraint if missing
do $$
begin
  if not exists (
    select 1
    from pg_constraint
    where conname = 'user_vpn_user_id_unique'
      and conrelid = 'public.user_vpn'::regclass
  ) then
    alter table public.user_vpn
      add constraint user_vpn_user_id_unique unique (user_id);
  end if;
end $$;
