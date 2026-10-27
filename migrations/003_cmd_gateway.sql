-- cmd.zeuglab.com reaches project memory through PostgREST (the public ZME front door), as role `zme_ws_caller`.
-- Apply as `postgres` after 001. Idempotent.
--
-- cmd holds one JWT whose role is `zme_ws_caller` (minted once with the PostgREST secret; cmd never holds the secret).
-- That role can execute exactly two functions and nothing else: no table, no other function, no Zeug memory.
--   public.zme_ws_call   the workspace API for one call: status, search, sources, fetch, add
--   public.zme_ws_setup  turn on memory for a project (creates its workspace)
-- cmd decides who may use which project and at which level; it passes the person's email as the actor.
-- The work runs as `zme_ws_owner`, a member of every workspace role, after declaring the one workspace for this
-- call, so forced RLS still shows only that workspace's rows. A leaked cmd JWT reaches client workspaces, never Zeug.

begin;

do $$
begin
  if not exists (select 1 from pg_roles where rolname = 'zme_ws_caller') then create role zme_ws_caller nologin; end if;
end $$;
grant zme_ws_caller to authenticator;                              -- PostgREST switches to it from the JWT
grant zme_ws_caller to postgres with inherit false, set true;      -- tests
grant usage on schema zme_ws to zme_ws_caller;

create or replace function zme_ws.call(p_workspace text, p_level text, p_actor text, p_op text, p_args jsonb)
returns jsonb
language plpgsql security definer set search_path = pg_catalog, extensions
as $fn$
declare a jsonb := coalesce(p_args, '{}'::jsonb); r jsonb; v extensions.vector;
begin
  if p_level is null or p_level not in ('read', 'write') then
    raise exception 'level must be read or write' using errcode = '22023';
  end if;
  if p_op = 'add' and p_level <> 'write' then
    raise exception 'this access is read-only' using errcode = '42501';
  end if;
  perform set_config('zme.workspace', coalesce(p_workspace, ''), true);
  perform set_config('zme.actor', coalesce(p_actor, ''), true);

  if p_op = 'status' then
    if not exists (select 1 from zme_ws.workspaces where id = p_workspace) then
      return jsonb_build_object('enabled', false);
    end if;
    return jsonb_build_object('enabled', true) || zme_ws.stats(p_workspace);
  elsif p_op = 'search' then
    if jsonb_typeof(a->'embedding') = 'array' then v := (a->'embedding')::text::extensions.vector; end if;
    select coalesce(jsonb_agg(to_jsonb(x) - 'content' || jsonb_build_object('content', left(x.content, 1200))), '[]'::jsonb) into r
    from zme_ws.search(p_workspace, v, nullif(btrim(a->>'q'), ''), least(coalesce((a->>'k')::int, 8), 25)) x;
    return jsonb_build_object('results', r);
  elsif p_op = 'sources' then
    select coalesce(jsonb_agg(to_jsonb(x)), '[]'::jsonb) into r
    from zme_ws.sources(p_workspace, least(coalesce((a->>'limit')::int, 50), 200)) x;
    return jsonb_build_object('sources', r);
  elsif p_op = 'fetch' then
    select to_jsonb(x) into r from zme_ws.fetch(p_workspace, (a->>'chunk_id')::uuid) x;
    return jsonb_build_object('chunk', r);
  elsif p_op = 'add' then
    return jsonb_build_object('id', zme_ws.add_source(p_workspace, a->>'title', a->>'text', a->'chunks', a->>'url',
                                                       coalesce(a->>'source_type', 'note'), coalesce(a->'metadata', '{}'::jsonb)));
  end if;
  raise exception 'unknown operation %', p_op using errcode = '22023';
end $fn$;
alter function zme_ws.call(text, text, text, text, jsonb) owner to zme_ws_owner;
revoke all on function zme_ws.call(text, text, text, text, jsonb) from public;
grant execute on function zme_ws.call(text, text, text, text, jsonb) to zme_ws_caller;

-- PostgREST only exposes `public`. Supabase's default privileges grant every new function here to anon,
-- authenticated and service_role, so each grant is revoked by name.
create or replace function public.zme_ws_call(p_workspace text, p_level text, p_actor text, p_op text,
                                              p_args jsonb default '{}'::jsonb)
returns jsonb
language sql security invoker set search_path = pg_catalog
as $$ select zme_ws.call(p_workspace, p_level, p_actor, p_op, p_args) $$;
revoke all on function public.zme_ws_call(text, text, text, text, jsonb) from public, anon, authenticated, service_role;
grant execute on function public.zme_ws_call(text, text, text, text, jsonb) to zme_ws_caller;

create or replace function public.zme_ws_setup(p_workspace text, p_title text)
returns jsonb
language plpgsql security definer set search_path = pg_catalog
as $$
begin
  perform zme_ws.create_workspace(p_workspace, p_title);   -- validates the id; idempotent
  return jsonb_build_object('workspace', p_workspace, 'enabled', true);
end $$;
revoke all on function public.zme_ws_setup(text, text) from public, anon, authenticated, service_role;
grant execute on function public.zme_ws_setup(text, text) to zme_ws_caller;

commit;

notify pgrst, 'reload schema';
