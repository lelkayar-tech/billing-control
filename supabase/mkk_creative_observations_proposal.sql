-- Proposed migration: not applied to production.
-- Isolated, server-side MKK creative observations; finance tables are untouched.
create table if not exists public.mkk_creative_observations (
    source_application_id bigint not null references public.applications(id),
    observation_id text not null,
    parent_application_id bigint not null references public.applications(id),
    source_document_number text not null,
    source_document_status text not null,
    place_id text,
    record jsonb not null,
    imported_at timestamptz not null default now(),
    primary key (source_application_id, observation_id)
);
create index if not exists mkk_creatives_parent_idx
    on public.mkk_creative_observations(parent_application_id);
alter table public.mkk_creative_observations enable row level security;
revoke all on public.mkk_creative_observations from anon, authenticated;
-- The FastAPI server uses a secret key. No direct browser access is granted.
