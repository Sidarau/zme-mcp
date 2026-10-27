-- ZEUG-795: client workspaces on omarchy ZME. One schema per cmd project, row-level security forced on every table.
--
-- Apply as `postgres` on the ZME database (tailnet 100.118.214.24:54322). Idempotent: re-running it re-applies the
-- layout to every existing workspace (upgrade_all at the end). Run `002_net_hardening.sql` once as the superuser too, then `tests/workspace_isolation.sql`.
--
-- Two independent locks keep a workspace's data in that workspace:
--   1. Privileges. Workspace `x` lives in schema `ws_x`. Only `zme_ws_x_read` (and `zme_ws_x_write`, which includes
--      it) can use that schema. No workspace role holds any privilege on Zeug's own memory in `public`.
--   2. Row-level security, forced, keyed to the workspace the session declared (`zme.workspace`). A role that was
--      wrongly granted another workspace's tables still sees no rows unless the session also declares that workspace.
-- Who reaches workspaces: cmd, through the narrow API in 003 (one call names one workspace), and administrators and
-- tooling connected as postgres. No login role can switch into every workspace: `zme_ws_gateway` is reserved for
-- ZEUG-796 and holds no privileges or memberships.

begin;

do $$
begin
  -- Owns every workspace object. Never logs in and has no BYPASSRLS, so forced RLS binds it too.
  if not exists (select 1 from pg_catalog.pg_roles where rolname = 'zme_ws_owner') then create role zme_ws_owner nologin; end if;
  -- Every workspace read role belongs to it: that is what lets it call the API in schema zme_ws.
  if not exists (select 1 from pg_catalog.pg_roles where rolname = 'zme_ws_member') then create role zme_ws_member nologin; end if;
  -- Reserved for the MCP server (ZEUG-796). No privileges, no memberships.
  if not exists (select 1 from pg_catalog.pg_roles where rolname = 'zme_ws_gateway') then create role zme_ws_gateway nologin; end if;
end $$;

-- postgres creates objects and hands them to the owner role; it already bypasses RLS, so this grants it nothing new.
grant zme_ws_owner to postgres with inherit true, set true;
grant usage on schema extensions to zme_ws_owner, zme_ws_member;   -- vector type, HNSW and RUM operators

create schema if not exists zme_ws authorization zme_ws_owner;
revoke all on schema zme_ws from public;
grant usage on schema zme_ws to zme_ws_member;
-- The owner runs the cmd gateway (003) as a member of every workspace's write role, so forced RLS still applies to it.
grant zme_ws_member to zme_ws_owner;

-- Registry ----------------------------------------------------------------------------------------------------------

create table if not exists zme_ws.workspaces (
  id          text primary key check (id ~ '^[a-z0-9][a-z0-9-]{1,40}$' and id <> 'internal'),  -- cmd PROJECT_RE
  schema_name text not null unique,
  title       text not null,
  created_at  timestamptz not null default now(),
  created_by  text not null default current_user
);
alter table zme_ws.workspaces owner to zme_ws_owner;
alter table zme_ws.workspaces enable row level security;
alter table zme_ws.workspaces force row level security;
revoke all on zme_ws.workspaces from public;
grant select on zme_ws.workspaces to zme_ws_member;

-- Helpers -------------------------------------------------------------------------------------------------------------

create or replace function zme_ws.current_workspace() returns text
language sql stable set search_path = pg_catalog, pg_temp
as $$ select nullif(current_setting('zme.workspace', true), '') $$;

create or replace function zme_ws.schema_for(p_id text) returns text
language sql immutable set search_path = pg_catalog, pg_temp
as $$ select 'ws_' || replace(p_id, '-', '_') $$;

create or replace function zme_ws.role_for(p_id text, p_level text) returns text
language sql immutable set search_path = pg_catalog, pg_temp
as $$ select 'zme_ws_' || replace(p_id, '-', '_') || '_' || p_level $$;

-- Same OR-over-lexemes query as public.zme_tsquery (websearch_to_tsquery ANDs every term and returns nothing for
-- natural-language questions). A copy, so workspace roles never need anything from Zeug's schema.
create or replace function zme_ws.tsquery(p_text text) returns tsquery
language sql immutable set search_path = pg_catalog, pg_temp
as $$
  select nullif(array_to_string(array(select distinct lexeme from unnest(to_tsvector('english', coalesce(p_text, '')))), ' | '), '')::tsquery
$$;

drop policy if exists own_row on zme_ws.workspaces;
create policy own_row on zme_ws.workspaces for select to zme_ws_member
  using (id = zme_ws.current_workspace() and pg_catalog.pg_has_role(current_user, 'zme_ws_' || replace(id, '-', '_') || '_read', 'USAGE'));

-- The schema of the session's own workspace, or an error. Every API function starts here, so a caller can neither
-- name a schema directly nor act on a workspace other than the one its session declared.
create or replace function zme_ws._schema(p_workspace text) returns text
language plpgsql stable set search_path = pg_catalog, pg_temp
as $$
declare s text;
begin
  if p_workspace is null or p_workspace is distinct from zme_ws.current_workspace() then
    raise exception 'workspace % is not this session''s workspace', p_workspace using errcode = '42501';
  end if;
  select schema_name into s from zme_ws.workspaces where id = p_workspace;
  if s is null then
    raise exception 'no workspace %', p_workspace using errcode = '42704';
  end if;
  return s;
end $$;

-- Provisioning (postgres only) -------------------------------------------------------------------------------------

create or replace function zme_ws.provision(p_id text) returns text
language plpgsql set search_path = pg_catalog, extensions, pg_temp
as $fn$
declare
  s  text := zme_ws.schema_for(p_id);
  rr text := zme_ws.role_for(p_id, 'read');
  rw text := zme_ws.role_for(p_id, 'write');
  t  text;
begin
  if p_id !~ '^[a-z0-9][a-z0-9-]{1,40}$' or p_id = 'internal' then
    raise exception 'workspace id % is not a client project id', p_id;
  end if;

  if not exists (select 1 from pg_catalog.pg_roles where rolname = rr) then execute format('create role %I nologin', rr); end if;
  if not exists (select 1 from pg_catalog.pg_roles where rolname = rw) then execute format('create role %I nologin', rw); end if;
  execute format('grant %I to %I', rr, rw);                                 -- write includes read
  execute format('grant zme_ws_member to %I', rr);
  execute format('grant %I, %I to postgres with inherit false, set true', rr, rw);   -- admin and tests
  execute format('grant %I to zme_ws_owner', rw);                          -- cmd gateway (003); RLS still applies

  execute format('create schema if not exists %I authorization zme_ws_owner', s);
  execute format('revoke all on schema %I from public', s);
  execute format('grant usage on schema %I to %I', s, rr);

  execute format($t$
    create table if not exists %1$I.sources (
      id           uuid primary key default gen_random_uuid(),
      workspace    text not null default %2$L check (workspace = %2$L),
      url          text unique,
      title        text not null,
      source_type  text not null default 'note',
      summary      text,
      raw_content  text not null,
      content_hash text not null unique,
      metadata     jsonb not null default '{}'::jsonb,
      origin       text,           -- 'zeug:<source id>' when the owner copied it in from Zeug's memory
      created_by   text default coalesce(nullif(current_setting('zme.actor', true), ''), current_user),
      created_at   timestamptz not null default now()
    )$t$, s, p_id);
  execute format($t$
    create table if not exists %1$I.chunks (
      id              uuid primary key default gen_random_uuid(),
      workspace       text not null default %2$L check (workspace = %2$L),
      source_id       uuid not null references %1$I.sources(id) on delete cascade,
      chunk_index     int not null,
      content         text not null,
      context_prefix  text,
      heading         text,
      token_count     int,
      embedding_v     extensions.vector(1024),
      embed_variant   text,
      chunker_version text,
      -- The prefix is searchable lexically but never embedded (a constant prefix pulls a source's chunks together).
      content_tsv     tsvector generated always as
                        (to_tsvector('english'::regconfig, coalesce(context_prefix, '') || ' ' || content)) stored,
      created_at      timestamptz not null default now(),
      unique (source_id, chunk_index)
    )$t$, s, p_id);
  -- Both arms need their own index: HNSW for cosine order, RUM for ranked lexical order (GIN cannot rank).
  execute format('create index if not exists chunks_embedding_hnsw on %I.chunks using hnsw (embedding_v extensions.vector_cosine_ops)', s);
  execute format('create index if not exists chunks_tsv_rum on %I.chunks using rum (content_tsv extensions.rum_tsvector_ops)', s);
  execute format('create index if not exists chunks_source_idx on %I.chunks (source_id)', s);
  execute format('create index if not exists sources_type_idx on %I.sources (source_type)', s);

  execute format('alter table %I.sources owner to zme_ws_owner', s);
  execute format('alter table %I.chunks owner to zme_ws_owner', s);
  execute format('revoke all on %I.sources, %I.chunks from public', s, s);
  execute format('grant select on %I.sources, %I.chunks to %I', s, s, rr);
  execute format('grant insert on %I.sources, %I.chunks to %I', s, s, rw);   -- add only: no update, no delete

  execute format('alter table %I.sources enable row level security', s);
  execute format('alter table %I.sources force row level security', s);
  execute format('alter table %I.chunks enable row level security', s);
  execute format('alter table %I.chunks force row level security', s);
  foreach t in array array['sources', 'chunks'] loop
    execute format('drop policy if exists ws_read on %I.%I', s, t);
    execute format('drop policy if exists ws_add on %I.%I', s, t);
    execute format($p$create policy ws_read on %1$I.%2$I for select to %3$I
                        using (workspace = %4$L and zme_ws.current_workspace() = %4$L)$p$,
                   s, t, rr, p_id);
    execute format($p$create policy ws_add on %1$I.%2$I for insert to %3$I
                        with check (workspace = %4$L and zme_ws.current_workspace() = %4$L)$p$,
                   s, t, rw, p_id);
  end loop;
  return s;
end $fn$;

create or replace function zme_ws.create_workspace(p_id text, p_title text) returns text
language plpgsql set search_path = pg_catalog, pg_temp
as $$
declare s text := zme_ws.provision(p_id);
begin
  insert into zme_ws.workspaces (id, schema_name, title) values (p_id, s, coalesce(nullif(btrim(p_title), ''), p_id))
  on conflict (id) do nothing;   -- setting up an existing workspace changes nothing
  return s;
end $$;

-- Re-applies the current layout to every workspace (run after changing provision()).
create or replace function zme_ws.upgrade_all() returns int
language plpgsql set search_path = pg_catalog, pg_temp
as $$
declare w record; n int := 0;
begin
  for w in select id from zme_ws.workspaces order by id loop
    perform zme_ws.provision(w.id);
    n := n + 1;
  end loop;
  return n;
end $$;

-- Deletes a workspace and everything in it. p_confirm must repeat the id.
create or replace function zme_ws.drop_workspace(p_id text, p_confirm text) returns void
language plpgsql set search_path = pg_catalog, pg_temp
as $$
declare s text;
begin
  if p_confirm is distinct from p_id then raise exception 'confirm by passing the workspace id twice'; end if;
  select schema_name into s from zme_ws.workspaces where id = p_id;
  if s is null then raise exception 'no workspace %', p_id; end if;
  execute format('drop schema %I cascade', s);
  execute format('drop role if exists %I', zme_ws.role_for(p_id, 'write'));
  execute format('drop role if exists %I', zme_ws.role_for(p_id, 'read'));
  delete from zme_ws.workspaces where id = p_id;
end $$;

-- The owner copies one source from Zeug's memory into a workspace (never live access). Vectors come along: both
-- sides use the arctic-l space. Zeug's metadata stays behind; the copy records only where it came from.
create or replace function zme_ws.copy_from_zeug(p_workspace text, p_source_id uuid) returns uuid
language plpgsql set search_path = pg_catalog, extensions, pg_temp
as $fn$
declare s text; v_id uuid; src public.sources;
begin
  select schema_name into s from zme_ws.workspaces where id = p_workspace;
  if s is null then raise exception 'no workspace %', p_workspace; end if;
  select * into src from public.sources where id = p_source_id;
  if not found then raise exception 'no Zeug source %', p_source_id; end if;
  execute format($q$insert into %I.sources (url, title, source_type, summary, raw_content, content_hash, metadata, origin)
                    values ($1, $2, $3, $4, $5, $6, $7, $8) on conflict do nothing returning id$q$, s)
    into v_id
    using src.url, coalesce(nullif(src.title, ''), 'Untitled'), coalesce(src.source_type, 'document'), src.summary,
          coalesce(src.raw_content, ''), coalesce(src.content_hash, encode(sha256(convert_to(coalesce(src.raw_content, ''), 'UTF8')), 'hex')),
          jsonb_build_object('copied_from', 'zeug', 'copied_at', now()), 'zeug:' || src.id;
  if v_id is null then
    raise exception 'workspace % already has this source (same URL or text)', p_workspace using errcode = '23505';
  end if;
  execute format($q$insert into %I.chunks (source_id, chunk_index, content, context_prefix, heading, token_count,
                                            embedding_v, embed_variant, chunker_version)
                    select $1, chunk_index, content, context_prefix, heading, token_count, embedding_v, embed_variant, chunker_version
                    from public.chunks where source_id = $2 and content is not null$q$, s)
    using v_id, src.id;
  return v_id;
end $fn$;

-- API (workspace roles) --------------------------------------------------------------------------------------------

-- Hybrid search inside the session's workspace: RRF over cosine (HNSW) and ranked lexical (RUM), filters pushed
-- into both arms. Lexical works without an embedding; pass q_embedding = null when none is available.
create or replace function zme_ws.search(
  p_workspace text, q_embedding extensions.vector default null, q_text text default null, k int default 8,
  rrf_k int default 60, candidates int default 60, p_source_types text[] default null)
returns table (chunk_id uuid, source_id uuid, source_title text, source_url text, source_type text, chunk_index int,
               content text, rrf_score double precision, vec_rank int, lex_rank int)
language plpgsql stable set search_path = pg_catalog, extensions, pg_temp
as $fn$
declare s text := zme_ws._schema(p_workspace);
begin
  return query execute format($q$
    with tq as (select zme_ws.tsquery($2) as q),
    vec as (
      select c.id, row_number() over (order by c.embedding_v <=> $1) as rank
      from %1$I.chunks c join %1$I.sources s on s.id = c.source_id
      where $1 is not null and c.embedding_v is not null and ($6 is null or s.source_type = any($6))
      order by c.embedding_v <=> $1
      limit $5 * 3),
    lex as (
      select c.id, row_number() over (order by c.content_tsv <=> (select q from tq)) as rank
      from %1$I.chunks c join %1$I.sources s on s.id = c.source_id
      where (select q from tq) is not null and c.content_tsv @@ (select q from tq) and ($6 is null or s.source_type = any($6))
      order by c.content_tsv <=> (select q from tq)
      limit $5 * 3),
    fused as (
      select coalesce(v.id, l.id) as id,
             (coalesce(1.0 / ($4 + v.rank), 0.0) + coalesce(1.0 / ($4 + l.rank), 0.0))::double precision as score,
             v.rank as vrank, l.rank as lrank
      from vec v full outer join lex l on l.id = v.id)
    select c.id, c.source_id, s.title, s.url, s.source_type, c.chunk_index, c.content, f.score, f.vrank::int, f.lrank::int
    from fused f join %1$I.chunks c on c.id = f.id join %1$I.sources s on s.id = c.source_id
    order by f.score desc, c.id
    limit $3$q$, s)
  using q_embedding, q_text, k, rrf_k, candidates, p_source_types;
end $fn$;

-- Adds one source with its chunks (chunked and embedded by the caller). Add-only: the same text or URL again returns
-- the existing id and changes nothing. p_chunks: [{chunk_index, content, context_prefix, heading, token_count,
-- embedding: [1024 floats] | null, embed_variant, chunker_version}].
create or replace function zme_ws.add_source(
  p_workspace text, p_title text, p_text text, p_chunks jsonb,
  p_url text default null, p_source_type text default 'note', p_metadata jsonb default '{}'::jsonb)
returns uuid
language plpgsql set search_path = pg_catalog, extensions, pg_temp
as $fn$
declare s text := zme_ws._schema(p_workspace); v_id uuid; v_hash text;
begin
  if p_text is null or length(btrim(p_text)) < 20 then raise exception 'text too short (min 20 characters)'; end if;
  if length(p_text) > 500000 then raise exception 'text too long (max 500,000 characters)'; end if;
  if jsonb_typeof(p_chunks) is distinct from 'array' or jsonb_array_length(p_chunks) = 0 then
    raise exception 'p_chunks must be a non-empty array';
  end if;
  v_hash := encode(sha256(convert_to(p_text, 'UTF8')), 'hex');
  execute format($q$insert into %I.sources (url, title, source_type, summary, raw_content, content_hash, metadata)
                    values ($1, $2, $3, $4, $5, $6, $7) on conflict do nothing returning id$q$, s)
    into v_id
    using p_url, left(coalesce(nullif(btrim(p_title), ''), left(p_text, 60)), 300), coalesce(p_source_type, 'note'),
          left(p_text, 400), p_text, v_hash, coalesce(p_metadata, '{}'::jsonb);
  if v_id is null then
    execute format('select id from %I.sources where content_hash = $1 or url = $2 limit 1', s) into v_id using v_hash, p_url;
    return v_id;
  end if;
  execute format($q$insert into %I.chunks (source_id, chunk_index, content, context_prefix, heading, token_count,
                                            embedding_v, embed_variant, chunker_version)
                    select $1, (c->>'chunk_index')::int, c->>'content', c->>'context_prefix', c->>'heading',
                           (c->>'token_count')::int, case when jsonb_typeof(c->'embedding') = 'array' then (c->'embedding')::text::extensions.vector end,
                           c->>'embed_variant',
                           c->>'chunker_version'
                    from jsonb_array_elements($2) c$q$, s)
    using v_id, p_chunks;
  return v_id;
end $fn$;

create or replace function zme_ws.fetch(p_workspace text, p_chunk_id uuid)
returns table (chunk_id uuid, source_id uuid, chunk_index int, content text, source_title text, source_url text,
               source_type text)
language plpgsql stable set search_path = pg_catalog, pg_temp
as $fn$
declare s text := zme_ws._schema(p_workspace);
begin
  return query execute format($q$select c.id, c.source_id, c.chunk_index, c.content, s.title, s.url, s.source_type
                                 from %1$I.chunks c join %1$I.sources s on s.id = c.source_id where c.id = $1$q$, s)
  using p_chunk_id;
end $fn$;

create or replace function zme_ws.sources(p_workspace text, p_limit int default 50)
returns table (id uuid, title text, source_type text, url text, origin text, created_by text, created_at timestamptz,
               chunks bigint)
language plpgsql stable set search_path = pg_catalog, pg_temp
as $fn$
declare s text := zme_ws._schema(p_workspace);
begin
  return query execute format($q$select s.id, s.title, s.source_type, s.url, s.origin, s.created_by, s.created_at,
                                        (select count(*) from %1$I.chunks c where c.source_id = s.id)
                                 from %1$I.sources s order by s.created_at desc limit least($1, 500)$q$, s)
  using p_limit;
end $fn$;

create or replace function zme_ws.stats(p_workspace text) returns jsonb
language plpgsql stable set search_path = pg_catalog, pg_temp
as $fn$
declare s text := zme_ws._schema(p_workspace); r jsonb;
begin
  execute format($q$select jsonb_build_object(
      'sources', (select count(*) from %1$I.sources),
      'chunks', (select count(*) from %1$I.chunks),
      'embedded', (select count(*) from %1$I.chunks where embedding_v is not null),
      'last_added_at', (select max(created_at) from %1$I.sources))$q$, s) into r;
  return r;
end $fn$;

-- Function privileges: nothing for PUBLIC; the API and RLS helpers for members; administration stays with postgres.
revoke all on all functions in schema zme_ws from public;
grant execute on function zme_ws.current_workspace(), zme_ws.tsquery(text), zme_ws._schema(text),
  zme_ws.search(text, extensions.vector, text, int, int, int, text[]),
  zme_ws.add_source(text, text, text, jsonb, text, text, jsonb),
  zme_ws.fetch(text, uuid), zme_ws.sources(text, int), zme_ws.stats(text)
  to zme_ws_member;

-- Earlier layouts let the gateway switch into every workspace role; remove those memberships.
do $$
declare m record;
begin
  for m in select r.rolname from pg_catalog.pg_auth_members a join pg_catalog.pg_roles r on r.oid = a.roleid
           where a.member = 'zme_ws_gateway'::regrole loop
    execute format('revoke %I from zme_ws_gateway', m.rolname);
  end loop;
end $$;

select zme_ws.upgrade_all();

commit;
