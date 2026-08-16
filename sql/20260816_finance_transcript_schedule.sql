-- Process one pending publisher transcript after each hourly discovery pass.

select cron.schedule(
    'zme-finance-transcripts-hourly',
    '18 * * * *',
    $job$
    select net.http_post(
        url := (
            select decrypted_secret from vault.decrypted_secrets
            where name = 'zme_project_url'
        ) || '/functions/v1/zme-finance-transcripts',
        headers := jsonb_build_object(
            'Content-Type', 'application/json',
            'apikey', (
                select decrypted_secret from vault.decrypted_secrets
                where name = 'zme_publishable_key'
            ),
            'Authorization', 'Bearer ' || (
                select decrypted_secret from vault.decrypted_secrets
                where name = 'zme_publishable_key'
            )
        ),
        body := jsonb_build_object('scheduled_at', now()),
        timeout_milliseconds := 120000
    ) as request_id;
    $job$
);
