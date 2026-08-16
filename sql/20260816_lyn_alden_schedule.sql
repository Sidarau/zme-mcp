-- Invoke the authenticated Lyn Alden public RSS/article collector daily. Vault
-- secrets are provisioned operationally and are not committed.

select cron.schedule(
    'zme-lyn-alden-daily',
    '27 7 * * *',
    $job$
    select net.http_post(
        url := (
            select decrypted_secret from vault.decrypted_secrets
            where name = 'zme_project_url'
        ) || '/functions/v1/zme-lyn-alden',
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
