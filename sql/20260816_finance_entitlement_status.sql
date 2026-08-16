-- Make known credential/licence gaps explicit instead of reporting them as
-- ambiguous collector absence. A later successful run naturally supersedes
-- these one-time bootstrap heartbeats.

with unavailable(source_key, reason) as (values
    ('bloomberg_surveillance', 'No licensed Bloomberg feed is configured'),
    ('reuters_markets', 'No licensed Reuters feed is configured'),
    ('bravos', 'No Bravos subscriber entitlement is configured'),
    ('financial_times', 'No licensed Financial Times feed is configured'),
    ('wall_street_journal', 'No licensed Wall Street Journal feed is configured'),
    ('options_positioning', 'No licensed options-positioning feed is configured')
)
insert into public.zme_ingestion_runs (
    source_key, started_at, finished_at, status, metadata
)
select u.source_key, now(), now(), 'not_entitled',
       jsonb_build_object('reason', u.reason, 'bootstrap', true)
from unavailable u
where not exists (
    select 1 from public.zme_ingestion_runs ir
    where ir.source_key = u.source_key
      and ir.status = 'not_entitled'
);
