-- ZEUG-795: keep workspace roles away from pg_net. Apply once as `supabase_admin` (the schema's owner).
--
-- On this database PUBLIC held USAGE on schema `net`, so every role, including each new workspace role, could send
-- HTTP requests from the database (net.http_post) and read other sessions' responses (net._http_response).
-- The roles that use pg_net (postgres, anon, authenticated, service_role, supabase_functions_admin) hold USAGE
-- through their own grants, so removing PUBLIC's changes nothing for them.

revoke usage on schema net from public;

-- Undo: grant usage on schema net to public;
