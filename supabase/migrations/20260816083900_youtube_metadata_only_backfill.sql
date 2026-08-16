-- Some legacy YouTube rows store a description in raw_content even though no
-- caption/transcript chunks exist. Treat them as metadata-only unless explicit
-- transcript provenance or searchable chunks prove otherwise.

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
  and s.transcript_provenance is null
  and not exists (
      select 1 from public.chunks ch where ch.source_id = s.id
  );
