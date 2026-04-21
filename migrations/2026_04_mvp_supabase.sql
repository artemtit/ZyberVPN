-- Run in Supabase SQL editor

create extension if not exists pgcrypto;

create table if not exists public.users (
  id uuid primary key default gen_random_uuid(),
  tg_id bigint not null unique,
  ref_tg_id bigint null,
  balance int not null default 0,
  trial_used boolean not null default false,
  promo_used boolean not null default false,
  vpn_key text,
  sub_token text unique,
  expires_at timestamptz,
  is_active boolean not null default true,
  plan text,
  last_activated_at timestamptz null,
  created_at timestamptz not null default now()
);

alter table if exists public.users add column if not exists ref_tg_id bigint null;
alter table if exists public.users add column if not exists balance int not null default 0;
alter table if exists public.users add column if not exists trial_used boolean not null default false;
alter table if exists public.users add column if not exists promo_used boolean not null default false;
alter table if exists public.users add column if not exists vpn_key text;
alter table if exists public.users add column if not exists sub_token text;
alter table if exists public.users add column if not exists expires_at timestamptz;
alter table if exists public.users add column if not exists is_active boolean not null default true;
alter table if exists public.users add column if not exists plan text;
alter table if exists public.users add column if not exists last_activated_at timestamptz null;

update public.users
set sub_token = encode(digest(sub_token, 'sha256'), 'hex')
where sub_token is not null
  and sub_token !~ '^[0-9a-f]{64}$';

create unique index if not exists idx_users_sub_token on public.users(sub_token);
create index if not exists idx_users_ref_tg_id on public.users(ref_tg_id);

create table if not exists public.keys (
  id bigint generated always as identity primary key,
  tg_id bigint not null,
  key text not null,
  created_at timestamptz not null default now()
);
create index if not exists idx_keys_tg_id on public.keys(tg_id);
create unique index if not exists idx_keys_tg_id_key_unique on public.keys(tg_id, key);

create table if not exists public.subscriptions (
  id bigint generated always as identity primary key,
  tg_id bigint not null,
  expires_at timestamptz not null,
  status text not null check (status in ('active', 'expired')),
  created_at timestamptz not null default now()
);
create index if not exists idx_subscriptions_tg_id on public.subscriptions(tg_id);
create index if not exists idx_subscriptions_status on public.subscriptions(status);

create table if not exists public.payments (
  id uuid primary key default gen_random_uuid(),
  tg_id bigint not null,
  amount int not null,
  status text not null,
  tariff_code text not null,
  email text null,
  payload text not null unique,
  idempotency_key text not null unique,
  telegram_payment_charge_id text null,
  created_at timestamptz not null default now()
);
create index if not exists idx_payments_tg_id on public.payments(tg_id);

create table if not exists public.idempotency_keys (
  operation text not null,
  idempotency_key text not null,
  status text not null,
  response_payload jsonb null,
  created_at timestamptz not null default now(),
  primary key (operation, idempotency_key)
);

create table if not exists public.servers (
  id bigint generated always as identity primary key,
  name text not null,
  host text not null,
  api_url text not null,
  username text not null,
  password text not null,
  inbound_id int not null,
  public_key text,
  short_id text,
  country text not null default 'unknown',
  is_active boolean not null default true,
  sni text,
  public_port int not null default 443,
  ws_path text not null default '/ws',
  ws_host text,
  last_health_check timestamptz,
  health_errors int not null default 0,
  last_error text
);

create table if not exists public.user_vpn (
  id bigint generated always as identity primary key,
  user_id bigint not null,
  server_id bigint not null references public.servers(id),
  reality_uuid text not null,
  ws_uuid text,
  reality_config text not null,
  ws_config text not null,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  unique (user_id)
);

create unique index if not exists idx_user_vpn_user_id on public.user_vpn(user_id);
create index if not exists idx_user_vpn_server_id on public.user_vpn(server_id);
