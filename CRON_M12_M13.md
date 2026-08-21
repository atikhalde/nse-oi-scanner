# M12 / M13 cron-job.org configuration

GitHub **workflow dispatch URLs only accept POST**. A GET (cron-job.org default
"monitor URL") returns **HTTP 404**. That is not a missing workflow.

Internal weekday schedules on `main` already run the paper cycles. Prefer those.
If you keep an external backup, use **POST** only.

## Preferred backup: repository_dispatch (stable, no workflow filename)

`POST https://api.github.com/repos/atikhalde/nse-oi-scanner/dispatches`

Headers:

- `Accept: application/vnd.github+json`
- `Authorization: Bearer YOUR_FINE_GRAINED_PAT`
- `X-GitHub-Api-Version: 2022-11-28`
- `Content-Type: application/json`

Body:

```json
{"event_type":"live-m12"}
```

```json
{"event_type":"live-m13"}
```

Success is **204 No Content**.

Event types: `live-m1`, `live-m2`, `live-m5` … `live-m13` (also short aliases `m12`, `m13`, …).

## Alternate: workflow_dispatch (must be POST, not GET)

- M12: `https://api.github.com/repos/atikhalde/nse-oi-scanner/actions/workflows/12_live_m12.yml/dispatches`
- M13: `https://api.github.com/repos/atikhalde/nse-oi-scanner/actions/workflows/13_live_m13.yml/dispatches`

Body:

```json
{"ref":"main","inputs":{"mode":"live"}}
```

`mode` is optional and defaults to `live`.

## Status codes

- `204`: accepted
- `404` on GET: wrong method — switch the job to POST
- `404` on POST: token cannot see the repo, or workflow not on `main`
- `401`: invalid/revoked token
- `403`: token lacks Actions write
- `422`: bad JSON / ref

Never put the token in the URL, repository, request body, issue, log, or chat.

## Manual Actions pages

- M12: `https://github.com/atikhalde/nse-oi-scanner/actions/workflows/12_live_m12.yml`
- M13: `https://github.com/atikhalde/nse-oi-scanner/actions/workflows/13_live_m13.yml`

## Schedule

Weekdays during NSE paper hours:

- UTC: 03:45–10:05
- India: 09:15–15:35

Do not run a full-frequency external cron **and** the repo's five-minute schedule
at the same time.
