-- MacroVoices publishes a stable podcast RSS feed whose episode descriptions
-- link to publisher-hosted transcript PDFs.

insert into public.channels (platform, handle, url_pattern, source_key)
select 'podcast', 'MacroVoices',
       'https://feeds.feedburner.com/MacroVoices', 'macrovoices'
where not exists (
    select 1 from public.channels
    where url_pattern = 'https://feeds.feedburner.com/MacroVoices'
);

update public.channels
set source_key = 'macrovoices'
where url_pattern = 'https://feeds.feedburner.com/MacroVoices';
