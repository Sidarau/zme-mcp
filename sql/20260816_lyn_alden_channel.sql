-- Lyn Alden exposes public article metadata through WordPress RSS and public
-- article bodies on the publisher site.

insert into public.channels (platform, handle, url_pattern, source_key)
select 'rss', 'Lyn Alden', 'https://www.lynalden.com/feed/', 'lyn_alden'
where not exists (
    select 1 from public.channels
    where url_pattern = 'https://www.lynalden.com/feed/'
);

update public.channels
set source_key = 'lyn_alden'
where url_pattern = 'https://www.lynalden.com/feed/';
