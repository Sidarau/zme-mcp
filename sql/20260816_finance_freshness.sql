-- Additive ZME source freshness/coverage contract.
-- No existing publication dates are guessed from ingestion timestamps.

begin;

create table if not exists public.zme_source_registry (
    source_key text primary key,
    display_name text not null,
    category text not null,
    platform text not null,
    locator text,
    enabled boolean not null default true,
    required boolean not null default false,
    expected_cadence_hours integer check (expected_cadence_hours > 0),
    stale_after_hours integer not null check (stale_after_hours > 0),
    access_mode text not null default 'public',
    metadata jsonb not null default '{}'::jsonb,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);

create table if not exists public.zme_ingestion_runs (
    run_id uuid primary key default gen_random_uuid(),
    source_key text not null references public.zme_source_registry(source_key),
    started_at timestamptz not null,
    finished_at timestamptz,
    status text not null check (status in (
        'running', 'success', 'error', 'not_entitled', 'disabled'
    )),
    items_seen integer not null default 0 check (items_seen >= 0),
    items_inserted integer not null default 0 check (items_inserted >= 0),
    newest_published_at timestamptz,
    cursor_before text,
    cursor_after text,
    error_class text,
    error_message text,
    metadata jsonb not null default '{}'::jsonb,
    created_at timestamptz not null default now(),
    check (finished_at is null or finished_at >= started_at)
);

create index if not exists zme_ingestion_runs_source_finished_idx
    on public.zme_ingestion_runs (source_key, finished_at desc);

alter table public.sources
    add column if not exists source_key text,
    add column if not exists published_at timestamptz,
    add column if not exists ingested_at timestamptz not null default now(),
    add column if not exists platform_item_id text,
    add column if not exists text_status text not null default 'ready',
    add column if not exists transcript_provenance text;

-- Backfill only machine-readable publication instants already present in the
-- corpus. Human-relative labels such as "3 weeks ago" remain unknown rather
-- than being guessed from ingestion time.
update public.sources
set published_at = (metadata->>'published_at')::timestamptz
where published_at is null
  and jsonb_typeof(metadata) = 'object'
  and metadata->>'published_at' ~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}([ T]|$)'
  and pg_input_is_valid(metadata->>'published_at', 'timestamptz');

update public.sources
set platform_item_id = coalesce(
        metadata->>'video_id',
        case when jsonb_typeof(metadata->'youtube_ids') = 'array'
             then metadata->'youtube_ids'->>0 end
    ),
    transcript_provenance = coalesce(
        transcript_provenance, metadata->>'transcript_provider'
    ),
    text_status = case metadata->>'transcript_status'
        when 'fetched' then 'ready'
        when 'pending' then 'pending'
        when 'empty' then 'unavailable'
        else text_status
    end
where jsonb_typeof(metadata) = 'object';

create index if not exists sources_source_key_published_idx
    on public.sources (source_key, published_at desc);
create unique index if not exists sources_platform_identity_idx
    on public.sources (source_key, platform_item_id)
    where source_key is not null and platform_item_id is not null;

alter table public.zme_source_registry enable row level security;
alter table public.zme_ingestion_runs enable row level security;

do $$
begin
    if not exists (
        select 1 from pg_policies
        where schemaname = 'public' and tablename = 'zme_source_registry'
          and policyname = 'zme_source_registry_read'
    ) then
        create policy zme_source_registry_read on public.zme_source_registry
            for select to anon, authenticated using (true);
    end if;
    if not exists (
        select 1 from pg_policies
        where schemaname = 'public' and tablename = 'zme_ingestion_runs'
          and policyname = 'zme_ingestion_runs_read'
    ) then
        create policy zme_ingestion_runs_read on public.zme_ingestion_runs
            for select to anon, authenticated using (true);
    end if;
end $$;

create or replace function public.zme_source_inventory(
    p_source_keys text[] default null,
    p_published_after timestamptz default null,
    p_as_of timestamptz default now()
)
returns table (
    source_key text,
    display_name text,
    category text,
    required boolean,
    state text,
    collector_health text,
    latest_poll_at timestamptz,
    latest_success_at timestamptz,
    latest_published_at timestamptz,
    documents_total bigint,
    documents_in_window bigint,
    items_7d bigint,
    undated_documents bigint,
    full_text_in_window bigint
)
language sql
stable
security invoker
set search_path = public
as $$
with configured as (
    select r.*
    from zme_source_registry r
    where r.enabled
      and (p_source_keys is null or r.source_key = any(p_source_keys))
), run_stats as (
    select c.source_key,
           coalesce(latest.finished_at, observed.last_checked_at) as latest_poll_at,
           latest.finished_at as ledger_poll_at,
           latest.status as latest_status,
           success.finished_at as latest_success_at
    from configured c
    left join lateral (
        select ir.finished_at, ir.status
        from zme_ingestion_runs ir
        where ir.source_key = c.source_key and ir.finished_at is not null
        order by ir.finished_at desc limit 1
    ) latest on true
    left join lateral (
        select ir.finished_at
        from zme_ingestion_runs ir
        where ir.source_key = c.source_key and ir.status = 'success'
          and ir.finished_at is not null
        order by ir.finished_at desc limit 1
    ) success on true
    left join lateral (
        select max(ch.last_checked_at) as last_checked_at
        from channels ch
        where ch.source_key = c.source_key
    ) observed on true
), content_stats as (
    select c.source_key,
           count(s.id) as documents_total,
           count(*) filter (
               where s.published_at >= coalesce(p_published_after, '-infinity'::timestamptz)
                 and s.published_at <= p_as_of
           ) as documents_in_window,
           count(*) filter (
               where s.published_at >= p_as_of - interval '7 days'
                 and s.published_at <= p_as_of
           ) as items_7d,
           count(*) filter (where s.id is not null and s.published_at is null) as undated_documents,
           count(*) filter (
               where s.published_at >= coalesce(p_published_after, '-infinity'::timestamptz)
                 and s.published_at <= p_as_of and s.text_status = 'ready'
                 and s.url not like '%/shorts/%'
                 and exists (
                     select 1 from public.chunks ch where ch.source_id = s.id
                 )
           ) as full_text_in_window,
           max(s.published_at) filter (where s.published_at <= p_as_of)
               as latest_published_at
    from configured c
    left join sources s on s.source_key = c.source_key
    group by c.source_key
)
select c.source_key,
       c.display_name,
       c.category,
       c.required,
       case
           when cs.documents_total = 0 then 'not_present'
           when cs.latest_published_at is null then 'unknown_date'
           when cs.documents_in_window > 0 then 'content_current'
           else 'content_stale'
       end as state,
       case
           when rs.latest_poll_at is null then 'never_ingested'
           when rs.ledger_poll_at is null then 'outcome_not_exposed'
           when rs.latest_status = 'not_entitled' then 'not_entitled'
           when rs.latest_success_at is null then 'failing'
           when rs.latest_success_at < p_as_of - make_interval(hours => c.stale_after_hours)
               then case when rs.latest_status = 'success' then 'stale' else 'failing' end
           else 'healthy'
       end as collector_health,
       rs.latest_poll_at,
       rs.latest_success_at,
       cs.latest_published_at,
       cs.documents_total,
       cs.documents_in_window,
       cs.items_7d,
       cs.undated_documents,
       cs.full_text_in_window
from configured c
join run_stats rs using (source_key)
join content_stats cs using (source_key)
order by c.required desc, c.category, c.display_name;
$$;

grant select on public.zme_source_registry to anon, authenticated;

-- The inventory RPC needs only these operational fields. Keep error text and
-- per-run counters out of the public Data API surface.
revoke all on public.zme_ingestion_runs from anon, authenticated;
grant select (source_key, finished_at, status)
    on public.zme_ingestion_runs to anon, authenticated;
grant all on public.zme_source_registry, public.zme_ingestion_runs
    to service_role;

-- PostgreSQL grants function execution to PUBLIC by default.
revoke execute on function public.zme_source_inventory(text[], timestamptz, timestamptz)
    from public;
grant execute on function public.zme_source_inventory(text[], timestamptz, timestamptz)
    to anon, authenticated, service_role;

commit;
