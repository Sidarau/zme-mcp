-- Invoke the authenticated finance discovery Edge Function hourly. The two
-- referenced secrets are provisioned operationally, not committed:
--   zme_project_url, zme_publishable_key

select cron.schedule(
    'zme-finance-discovery-hourly',
    '12 * * * *',
    $job$
    select net.http_post(
        url := (
            select decrypted_secret from vault.decrypted_secrets
            where name = 'zme_project_url'
        ) || '/functions/v1/zme-finance-discovery',
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
        timeout_milliseconds := 30000
    ) as request_id;
    $job$
);
