-- Register the verified finance YouTube channels in the collector's existing
-- channel table and propagate canonical source keys to newly ingested rows.

begin;

alter table public.channels
    add column if not exists source_key text
        references public.zme_source_registry(source_key);

with desired(source_key, handle, channel_id) as (values
    ('forward_guidance', 'Forward Guidance', 'UCEOv-8wHvYC6mzsY2Gm5WcQ'),
    ('real_vision', 'Real Vision', 'UCBH5VZE_Y4F3CMcPIzPEB5A'),
    ('bloomberg_tv', 'Bloomberg Television', 'UCIALMKvObZNtJ6AmdCLP7Lg'),
    ('bloomberg_odd_lots', 'Bloomberg Podcasts', 'UChF5O40UBqAc82I7-i5ig6A'),
    ('cnbc_tv', 'CNBC Television', 'UCvJJ_dzjViJCoLf5uKUTwoA'),
    ('wealthion', 'Wealthion', 'UCKMeK-HGHfUFFArZ91rzv5A'),
    ('the_compound', 'The Compound', 'UCBRpqrzuuqE8TZcWw75JSdw'),
    ('all_in', 'All-In Podcast', 'UCESLZhusAkFfsNsApnjF_Cg')
)
insert into public.channels (platform, handle, url_pattern, source_key)
select 'youtube', d.handle,
       'https://www.youtube.com/feeds/videos.xml?channel_id=' || d.channel_id,
       d.source_key
from desired d
where not exists (
    select 1 from public.channels c
    where c.platform = 'youtube'
      and c.url_pattern =
          'https://www.youtube.com/feeds/videos.xml?channel_id=' || d.channel_id
);

with desired(source_key, channel_id) as (values
    ('forward_guidance', 'UCEOv-8wHvYC6mzsY2Gm5WcQ'),
    ('real_vision', 'UCBH5VZE_Y4F3CMcPIzPEB5A'),
    ('bloomberg_tv', 'UCIALMKvObZNtJ6AmdCLP7Lg'),
    ('bloomberg_odd_lots', 'UChF5O40UBqAc82I7-i5ig6A'),
    ('cnbc_tv', 'UCvJJ_dzjViJCoLf5uKUTwoA'),
    ('wealthion', 'UCKMeK-HGHfUFFArZ91rzv5A'),
    ('the_compound', 'UCBRpqrzuuqE8TZcWw75JSdw'),
    ('all_in', 'UCESLZhusAkFfsNsApnjF_Cg')
)
update public.channels c
set source_key = d.source_key
from desired d
where c.platform = 'youtube'
  and c.url_pattern =
      'https://www.youtube.com/feeds/videos.xml?channel_id=' || d.channel_id;

-- Preserve canonical identity even if a legacy collector knows only channel_id.
create or replace function public.zme_set_source_key_from_channel()
returns trigger
language plpgsql
security invoker
set search_path = public
as $$
begin
    if new.source_key is null and new.channel_id is not null then
        select c.source_key into new.source_key
        from public.channels c
        where c.id = new.channel_id;
    end if;
    return new;
end;
$$;

drop trigger if exists zme_sources_set_source_key on public.sources;
create trigger zme_sources_set_source_key
before insert or update of channel_id on public.sources
for each row execute function public.zme_set_source_key_from_channel();

revoke execute on function public.zme_set_source_key_from_channel() from public;

update public.sources s
set source_key = c.source_key
from public.channels c
where s.source_key is null
  and s.channel_id = c.id
  and c.source_key is not null;

commit;
