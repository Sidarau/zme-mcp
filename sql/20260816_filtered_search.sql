-- Apply publication/source eligibility before vector and lexical ranking.

drop function if exists public.zme_search_filtered(
    vector, text, integer, integer, integer, text[], timestamptz,
    timestamptz, text[]
);

create or replace function public.zme_search_filtered(
    q_embedding vector default null::vector,
    q_text text default null,
    k integer default 10,
    rrf_k integer default 60,
    candidates integer default 60,
    p_source_types text[] default null,
    p_published_after timestamptz default null,
    p_published_before timestamptz default null,
    p_source_keys text[] default null,
    p_exclude_short_form boolean default true
)
returns table (
    chunk_id uuid,
    source_id uuid,
    source_title text,
    source_url text,
    chunk_index integer,
    content text,
    context_prefix text,
    rrf_score double precision,
    vec_rank integer,
    lex_rank integer
)
language sql
stable
security invoker
set search_path = public, extensions
as $$
with tq as (
    select public.zme_tsquery(q_text) as q
), eligible as materialized (
    select s.id
    from public.sources s
    where (p_source_types is null or s.source_type = any(p_source_types))
      and (p_published_after is null or s.published_at >= p_published_after)
      and (p_published_before is null or s.published_at <= p_published_before)
      and (p_source_keys is null or s.source_key = any(p_source_keys))
      and (not p_exclude_short_form or s.url not like '%/shorts/%')
), vec as (
    select c.id,
           row_number() over (order by c.embedding_v <=> q_embedding) as rank
    from public.chunks c
    join eligible e on e.id = c.source_id
    where q_embedding is not null and c.embedding_v is not null
    order by c.embedding_v <=> q_embedding
    limit candidates * 3
), lex as (
    select c.id,
           row_number() over (
               order by c.content_tsv <=> (select q from tq)
           ) as rank
    from public.chunks c
    join eligible e on e.id = c.source_id
    where (select q from tq) is not null
      and c.content_tsv @@ (select q from tq)
    order by c.content_tsv <=> (select q from tq)
    limit candidates * 3
), fused as (
    select coalesce(v.id, l.id) as id,
           (coalesce(1.0 / (rrf_k + v.rank), 0.0)
            + coalesce(1.0 / (rrf_k + l.rank), 0.0))::double precision as score,
           v.rank as vrank,
           l.rank as lrank
    from vec v
    full outer join lex l on l.id = v.id
)
select c.id, c.source_id, s.title, s.url, c.chunk_index, c.content,
       c.context_prefix, f.score, f.vrank::int, f.lrank::int
from fused f
join public.chunks c on c.id = f.id
join public.sources s on s.id = c.source_id
order by f.score desc, c.id
limit k;
$$;

revoke execute on function public.zme_search_filtered(
    vector, text, integer, integer, integer, text[], timestamptz,
    timestamptz, text[], boolean
) from public;
grant execute on function public.zme_search_filtered(
    vector, text, integer, integer, integer, text[], timestamptz,
    timestamptz, text[], boolean
) to anon, authenticated, service_role;
