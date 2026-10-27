-- ZEUG-795 isolation regression test. Run as postgres after the migrations, and again after any change to zme_ws,
-- a workspace schema, its roles or policies:
--
--   psql "host=100.118.214.24 port=54322 dbname=postgres user=postgres" -X -v ON_ERROR_STOP=1 -f tests/workspace_isolation.sql
--
-- Creates two throwaway workspaces (A, B) and a canary in Zeug's own memory inside one transaction, tries every
-- crossing as the workspace roles, and rolls back, so nothing persists. The last line reads PASS or FAIL.

begin;
set local client_min_messages = warning;

select zme_ws.create_workspace('zz-isol-a', 'Isolation test A');
select zme_ws.create_workspace('zz-isol-b', 'Isolation test B');

-- One canary per workspace and one in Zeug's memory, all with the same vector, so a vector search that crossed a
-- boundary would find the other canaries first.
create temp table canary_vec as select array_fill(0.01::real, array[1024])::extensions.vector as v;

with s as (insert into ws_zz_isol_a.sources (title, raw_content, content_hash)
           values ('A canary', 'wscanaryalpha lives only in workspace A', 'test-a') returning id)
insert into ws_zz_isol_a.chunks (source_id, chunk_index, content, embedding_v)
select s.id, 0, 'wscanaryalpha lives only in workspace A', v from s, canary_vec;

with s as (insert into ws_zz_isol_b.sources (title, raw_content, content_hash)
           values ('B canary', 'wscanarybeta lives only in workspace B', 'test-b') returning id)
insert into ws_zz_isol_b.chunks (source_id, chunk_index, content, embedding_v)
select s.id, 0, 'wscanarybeta lives only in workspace B', v from s, canary_vec;

with s as (insert into public.sources (id, url, title, source_type, raw_content, content_hash)
           values (gen_random_uuid(), 'internal://test/zme-ws-isolation-canary', 'Zeug canary', 'note',
                   'zeugcanaryomega lives only in Zeug memory', 'test-zeug-canary') returning id)
insert into public.chunks (id, source_id, chunk_index, content, embedding_v)
select gen_random_uuid(), s.id, 0, 'zeugcanaryomega lives only in Zeug memory', v
from s, canary_vec;

-- Runs one statement as p_role with the session declaring workspace p_ws. Returns the count it produced, or
-- 'denied <sqlstate>'. An error rolls back the block, which also undoes the role switch.
create function pg_temp.probe(p_role text, p_ws text, p_sql text) returns text
language plpgsql as $$
declare n bigint;
begin
  execute format('set local role %I', p_role);
  perform set_config('zme.workspace', coalesce(p_ws, ''), true);
  execute p_sql into n;
  reset role;
  perform set_config('zme.workspace', '', true);
  return n::text;
exception when others then
  return 'denied ' || sqlstate;
end $$;

-- What a role can reach outside its own workspace schema, from the catalog rather than by trying.
-- extensions.pg_stat_statements(_info) is readable by PUBLIC; Postgres redacts other users' query text there.
create function pg_temp.reach(p_role text, p_own text) returns table (kind text, n bigint)
language sql as $$
  select 'foreign tables', count(*) from pg_class c join pg_namespace s on s.oid = c.relnamespace
  where c.relkind in ('r', 'v', 'm', 'p', 'f')
    and s.nspname !~ '^pg_' and s.nspname not in ('information_schema', 'zme_ws', coalesce(p_own, '-'))
    and not (s.nspname = 'extensions' and c.relname in ('pg_stat_statements', 'pg_stat_statements_info'))
    and has_schema_privilege(p_role, s.oid, 'USAGE')
    and has_table_privilege(p_role, c.oid, 'SELECT,INSERT,UPDATE,DELETE,TRUNCATE')
  union all
  select 'definer functions', count(*) from pg_proc p join pg_namespace s on s.oid = p.pronamespace
  where p.prosecdef and s.nspname not in ('pg_catalog', 'information_schema')
    and has_schema_privilege(p_role, s.oid, 'USAGE') and has_function_privilege(p_role, p.oid, 'EXECUTE')
  union all
  select 'schemas it can create in', count(*) from pg_namespace s
  where s.nspname !~ '^pg_' and has_schema_privilege(p_role, s.oid, 'CREATE')
  union all
  select 'pg_net usable', count(*) from pg_namespace s where s.nspname = 'net' and has_schema_privilege(p_role, s.oid, 'USAGE')
$$;

create temp table results (n serial, check_name text, got text, want text);

\set ar 'zme_ws_zz_isol_a_read'
\set aw 'zme_ws_zz_isol_a_write'

insert into results (check_name, got, want) values
-- The checks below are only meaningful if the workspace can see its own data.
('A reads its own canary',
 pg_temp.probe(:'ar', 'zz-isol-a', $q$select count(*) from ws_zz_isol_a.chunks where content like '%wscanaryalpha%'$q$), '1'),
('A finds its canary by lexical search',
 pg_temp.probe(:'ar', 'zz-isol-a', $q$select count(*) from zme_ws.search('zz-isol-a', null, 'wscanaryalpha')$q$), '1'),
('A vector search returns only A''s chunk',
 pg_temp.probe(:'ar', 'zz-isol-a', $q$select count(*) from zme_ws.search('zz-isol-a', array_fill(0.01::real, array[1024])::extensions.vector, null, 50)$q$), '1'),
('A sees only its own registry row',
 pg_temp.probe(:'ar', 'zz-isol-a', $q$select count(*) from zme_ws.workspaces$q$), '1'),

-- Cross-workspace reads
('A reads B''s table',
 pg_temp.probe(:'ar', 'zz-isol-a', $q$select count(*) from ws_zz_isol_b.chunks$q$), 'denied 42501'),
('A reads B''s table while declaring B',
 pg_temp.probe(:'ar', 'zz-isol-b', $q$select count(*) from ws_zz_isol_b.chunks$q$), 'denied 42501'),
('A searches B through the API',
 pg_temp.probe(:'ar', 'zz-isol-a', $q$select count(*) from zme_ws.search('zz-isol-b', null, 'wscanarybeta')$q$), 'denied 42501'),
('A searches B through the API while declaring B',
 pg_temp.probe(:'ar', 'zz-isol-b', $q$select count(*) from zme_ws.search('zz-isol-b', null, 'wscanarybeta')$q$), 'denied 42501'),

-- Spoofed or missing workspace: RLS hides A's own rows unless the session declares A
('A rows visible while declaring B',
 pg_temp.probe(:'ar', 'zz-isol-b', $q$select count(*) from ws_zz_isol_a.chunks$q$), '0'),
('A rows visible with no workspace declared',
 pg_temp.probe(:'ar', null, $q$select count(*) from ws_zz_isol_a.chunks$q$), '0'),

-- Cross-workspace and mislabelled writes
('A writer inserts into B',
 pg_temp.probe(:'aw', 'zz-isol-a', $q$with i as (insert into ws_zz_isol_b.sources (title, raw_content, content_hash) values ('x', 'cross write attempt', 'x1') returning 1) select count(*) from i$q$), 'denied 42501'),
('A writer inserts a row labelled B',
 pg_temp.probe(:'aw', 'zz-isol-a', $q$with i as (insert into ws_zz_isol_a.sources (workspace, title, raw_content, content_hash) values ('zz-isol-b', 'x', 'mislabelled row', 'x2') returning 1) select count(*) from i$q$), 'denied*'),
('A writer inserts into A while declaring B',
 pg_temp.probe(:'aw', 'zz-isol-b', $q$with i as (insert into ws_zz_isol_a.sources (title, raw_content, content_hash) values ('x', 'spoofed write', 'x3') returning 1) select count(*) from i$q$), 'denied 42501'),
('A writer adds to B through the API',
 pg_temp.probe(:'aw', 'zz-isol-a', $q$select count(*) from (select zme_ws.add_source('zz-isol-b', 't', 'cross write through the api', '[{"chunk_index":0,"content":"cross write"}]')) x$q$), 'denied 42501'),
('A reader adds to A',
 pg_temp.probe(:'ar', 'zz-isol-a', $q$select count(*) from (select zme_ws.add_source('zz-isol-a', 't', 'reader must not be able to add', '[{"chunk_index":0,"content":"reader add"}]')) x$q$), 'denied 42501'),
('A writer adds to A through the API',
 pg_temp.probe(:'aw', 'zz-isol-a', $q$select count(*) from (select zme_ws.add_source('zz-isol-a', 'Gamma', 'wsaddedgamma arrived through the api', '[{"chunk_index":0,"content":"wsaddedgamma arrived through the api"}]')) x$q$), '1'),
('A finds the added note',
 pg_temp.probe(:'ar', 'zz-isol-a', $q$select count(*) from zme_ws.search('zz-isol-a', null, 'wsaddedgamma')$q$), '1'),

-- Zeug's own memory
('Zeug canary through A''s lexical search',
 pg_temp.probe(:'ar', 'zz-isol-a', $q$select count(*) from zme_ws.search('zz-isol-a', null, 'zeugcanaryomega')$q$), '0'),
('Zeug canary through A''s vector search',
 pg_temp.probe(:'ar', 'zz-isol-a', $q$select count(*) from zme_ws.search('zz-isol-a', array_fill(0.01::real, array[1024])::extensions.vector, null, 50) where content like '%zeugcanaryomega%'$q$), '0'),
('A reads Zeug''s chunks',
 pg_temp.probe(:'ar', 'zz-isol-a', $q$select count(*) from public.chunks$q$), 'denied 42501'),
('A calls Zeug''s search',
 pg_temp.probe(:'ar', 'zz-isol-a', $q$select count(*) from public.zme_search_hybrid(null, 'zeugcanaryomega', 5)$q$), 'denied 42501'),
('A writer calls Zeug''s remember',
 pg_temp.probe(:'aw', 'zz-isol-a', $q$select count(*) from (select public.zme_remember('t', 'a workspace must not write to Zeug memory', 'x')) x$q$), 'denied 42501'),
('A writer sends HTTP from the database',
 pg_temp.probe(:'aw', 'zz-isol-a', $q$select count(*) from (select net.http_get('http://example.invalid')) x$q$), 'denied 42501'),

-- The gateway holds nothing until it switches to a workspace role
('Gateway reads A without switching',
 pg_temp.probe('zme_ws_gateway', 'zz-isol-a', $q$select count(*) from ws_zz_isol_a.chunks$q$), 'denied 42501'),
('Gateway calls the API without switching',
 pg_temp.probe('zme_ws_gateway', 'zz-isol-a', $q$select count(*) from zme_ws.search('zz-isol-a', null, 'wscanaryalpha')$q$), 'denied 42501');

insert into results (check_name, got, want)
select r.role || ': ' || x.kind, x.n::text, '0'
from (values ('zme_ws_zz_isol_a_read', 'ws_zz_isol_a'), ('zme_ws_zz_isol_a_write', 'ws_zz_isol_a'),
             ('zme_ws_gateway', null)) as r(role, own),
     lateral pg_temp.reach(r.role, r.own) x;

select n, check_name, got, want,
       case when got = want or (want = 'denied*' and got like 'denied %') then 'ok' else 'FAIL' end as result
from results order by n;

select case when bool_and(got = want or (want = 'denied*' and got like 'denied %')) then 'PASS' else 'FAIL' end
       || ' (' || count(*) || ' checks)' as zme_workspace_isolation
from results;

rollback;
