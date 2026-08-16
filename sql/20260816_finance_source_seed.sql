-- Idempotent initial finance source universe. Apply after the freshness migration.

insert into public.zme_source_registry (
    source_key, display_name, category, platform, locator, required,
    expected_cadence_hours, stale_after_hours, access_mode
) values
    ('kobeissi', 'Kobeissi Letter', 'macro_markets', 'web_x',
     'https://www.thekobeissiletter.com/', true, 24, 72, 'mixed'),
    ('lyn_alden', 'Lyn Alden', 'macro_markets', 'web',
     'https://www.lynalden.com/', true, 168, 336, 'public'),
    ('macrovoices', 'MacroVoices', 'macro_markets', 'podcast_web',
     'https://www.macrovoices.com/', true, 168, 336, 'public'),
    ('forward_guidance', 'Forward Guidance', 'macro_markets', 'youtube_podcast',
     'https://www.youtube.com/@ForwardGuidanceBW', true, 72, 168, 'public'),
    ('real_vision', 'Real Vision', 'macro_markets', 'web_youtube',
     'https://www.youtube.com/@RealVisionFinance', true, 72, 168, 'mixed'),
    ('bloomberg_tv', 'Bloomberg Television', 'macro_markets', 'youtube',
     'https://www.youtube.com/@markets', true, 12, 48, 'public_metadata'),
    ('bloomberg_surveillance', 'Bloomberg Surveillance', 'macro_markets', 'web_podcast',
     'https://www.bloomberg.com/live', true, 24, 72, 'licensed'),
    ('cnbc_tv', 'CNBC Television', 'macro_markets', 'youtube',
     'https://www.youtube.com/@CNBCtelevision', true, 12, 48, 'public_metadata'),
    ('reuters_markets', 'Reuters Markets', 'macro_markets', 'web',
     'https://www.reuters.com/markets/', true, 6, 24, 'licensed'),
    ('financial_times', 'Financial Times', 'macro_markets', 'web',
     'https://www.ft.com/markets', false, 12, 48, 'licensed'),
    ('wall_street_journal', 'Wall Street Journal', 'macro_markets', 'web',
     'https://www.wsj.com/news/markets', false, 12, 48, 'licensed'),
    ('bravos', 'Bravos', 'stocks_positioning', 'multi',
     'https://bravosresearch.com/', true, 24, 72, 'entitled'),
    ('finance_lancelot', 'FinanceLancelot', 'stocks_positioning', 'x',
     'https://x.com/FinanceLancelot', true, 24, 72, 'api'),
    ('sec_edgar', 'SEC EDGAR', 'stocks_positioning', 'api',
     'https://www.sec.gov/edgar/search-and-access', true, 6, 24, 'public_api'),
    ('company_ir', 'Company investor relations', 'stocks_positioning', 'web',
     null, true, 24, 72, 'public'),
    ('wealthion', 'Wealthion', 'youtube_podcast', 'youtube',
     'https://www.youtube.com/@Wealthion', false, 72, 168, 'public_metadata'),
    ('the_compound', 'The Compound', 'youtube_podcast', 'youtube',
     'https://www.youtube.com/@TheCompoundNews', false, 72, 168, 'public_metadata'),
    ('bloomberg_odd_lots', 'Bloomberg Podcasts / Odd Lots', 'youtube_podcast', 'youtube_podcast',
     'https://www.youtube.com/@BloombergPodcasts', false, 72, 168, 'mixed'),
    ('all_in', 'All-In Podcast', 'youtube_podcast', 'youtube_podcast',
     'https://www.youtube.com/@allin', false, 168, 336, 'public_metadata'),
    ('prediction_markets', 'Prediction markets', 'alternative_tactical', 'api',
     null, false, 6, 24, 'api'),
    ('options_positioning', 'Options positioning', 'alternative_tactical', 'api',
     null, false, 6, 24, 'licensed'),
    ('fund_flows', 'Fund flows', 'alternative_tactical', 'api_web',
     null, false, 24, 72, 'mixed'),
    ('crypto_research', 'Crypto-native research', 'alternative_tactical', 'multi',
     null, false, 24, 72, 'mixed'),
    ('commodities_specialists', 'Commodities specialists', 'alternative_tactical', 'multi',
     null, false, 24, 72, 'mixed')
on conflict (source_key) do update set
    display_name = excluded.display_name,
    category = excluded.category,
    platform = excluded.platform,
    locator = excluded.locator,
    required = excluded.required,
    expected_cadence_hours = excluded.expected_cadence_hours,
    stale_after_hours = excluded.stale_after_hours,
    access_mode = excluded.access_mode,
    updated_at = now();

-- Map the canonical publishers already represented in the legacy corpus.
-- Future collectors should write source_key directly.
with aliases(source_key, alias) as (values
    ('bravos', 'Bravos Research'),
    ('finance_lancelot', 'FinanceLancelot'),
    ('forward_guidance', 'Forward Guidance'),
    ('real_vision', 'Real Vision'),
    ('bloomberg_tv', 'Bloomberg Television'),
    ('cnbc_tv', 'CNBC Television'),
    ('wealthion', 'Wealthion'),
    ('the_compound', 'The Compound'),
    ('bloomberg_odd_lots', 'Bloomberg Podcasts'),
    ('all_in', 'All-In Podcast')
)
update public.sources s
set source_key = a.source_key
from aliases a
where s.source_key is null
  and jsonb_typeof(s.metadata) = 'object'
  and lower(coalesce(
      s.metadata->>'channel_name',
      s.metadata->>'channel',
      s.metadata->>'youtube_channel_handle',
      ''
  )) = lower(a.alias);

update public.sources
set source_key = 'finance_lancelot'
where source_key is null
  and lower(url) like 'x://financelancelot/%';

update public.sources
set source_key = 'bravos'
where source_key is null
  and lower(url) like '%bravosresearch.com%';
