-- Represent provider/configuration gaps explicitly. YouTube's supported
-- captions.download API requires OAuth authorization from the video owner;
-- current metadata collection alone must not be reported as searchable text.

alter table public.zme_ingestion_runs
    drop constraint zme_ingestion_runs_status_check;

alter table public.zme_ingestion_runs
    add constraint zme_ingestion_runs_status_check check (status in (
        'running', 'success', 'error', 'not_entitled',
        'configuration_required', 'disabled'
    ));

alter table public.sources
    drop constraint sources_transcript_status_check;

alter table public.sources
    add constraint sources_transcript_status_check check (transcript_status in (
        'fetched', 'empty', 'pending', 'provider_required'
    ));

with transcript_sources(source_key) as (values
    ('forward_guidance'),
    ('real_vision'),
    ('bloomberg_tv'),
    ('cnbc_tv'),
    ('bloomberg_odd_lots'),
    ('wealthion'),
    ('the_compound'),
    ('all_in')
)
update public.sources s
set text_status = 'metadata_only',
    transcript_status = 'provider_required',
    updated_at = now()
from transcript_sources ts
where s.source_key = ts.source_key
  and s.source_type = 'video'
  and coalesce(s.raw_content, '') = ''
  and not exists (
      select 1 from public.chunks ch where ch.source_id = s.id
  );

with unavailable(source_key, reason) as (values
    ('forward_guidance', 'Video metadata is current; an authorized transcript provider is not configured'),
    ('real_vision', 'An authorized transcript provider is not configured'),
    ('bloomberg_tv', 'Video metadata is current; an authorized transcript provider is not configured'),
    ('cnbc_tv', 'Video metadata is current; an authorized transcript provider is not configured'),
    ('bloomberg_odd_lots', 'Video metadata is current; an authorized transcript provider is not configured'),
    ('wealthion', 'Video metadata is current; an authorized transcript provider is not configured'),
    ('the_compound', 'Video metadata is current; an authorized transcript provider is not configured'),
    ('all_in', 'Video metadata is current; an authorized transcript provider is not configured'),
    ('finance_lancelot', 'An X API collector is not configured'),
    ('kobeissi', 'The newsletter is subscriber-only and no public X API collector is configured'),
    ('sec_edgar', 'A company/form watchlist is required before enabling EDGAR ingestion'),
    ('company_ir', 'A company watchlist is required before enabling investor-relations ingestion')
)
insert into public.zme_ingestion_runs (
    source_key, started_at, finished_at, status, metadata
)
select u.source_key, now(), now(), 'configuration_required',
       jsonb_build_object('stage', 'full_text', 'reason', u.reason, 'bootstrap', true)
from unavailable u
where not exists (
    select 1 from public.zme_ingestion_runs ir
    where ir.source_key = u.source_key
      and ir.status = 'configuration_required'
);

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
           when rs.latest_status = 'configuration_required' then 'configuration_required'
           when rs.latest_status = 'error' then 'failing'
           when rs.latest_status = 'disabled' then 'disabled'
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
