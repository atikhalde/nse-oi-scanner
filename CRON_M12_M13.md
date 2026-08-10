# M12 / M13 cron-job.org configuration

These endpoints become valid after `.github/workflows/12_live_m12.yml` and `13_live_m13.yml` are merged into `main`.

## Workflow dispatch endpoints

### M12

`https://api.github.com/repos/atikhalde/nse-oi-scanner/actions/workflows/12_live_m12.yml/dispatches`

### M13

`https://api.github.com/repos/atikhalde/nse-oi-scanner/actions/workflows/13_live_m13.yml/dispatches`

## Request

Method: `POST`

Headers:

- `Accept: application/vnd.github+json`
- `Authorization: Bearer YOUR_NEW_FINE_GRAINED_PAT`
- `X-GitHub-Api-Version: 2022-11-28`
- `Content-Type: application/json`

Body for both workflows:

```json
{"ref":"main","inputs":{"mode":"live"}}
```

Create a fresh fine-grained token restricted to this repository with Actions write permission. Never place the token in the URL, repository, request body, issue, log, or chat.

## Manual Actions pages

- M12: `https://github.com/atikhalde/nse-oi-scanner/actions/workflows/12_live_m12.yml`
- M13: `https://github.com/atikhalde/nse-oi-scanner/actions/workflows/13_live_m13.yml`

## Suggested schedule

Weekdays every five minutes during NSE paper hours:

- UTC: 03:45–10:05
- Bahrain: 06:45–13:05
- India: 09:15–15:35

Avoid running cron-job.org and the workflow's internal five-minute schedule simultaneously. Duplicate dispatches will be deduplicated at the alert layer, but they can queue jobs and cause otherwise-valid signals to become stale. Use one primary scheduler and one limited backup rather than two full-frequency schedulers.

## Successful response

GitHub workflow dispatch normally returns HTTP `204 No Content`.

- `404`: workflow is not yet on `main`, or token cannot see the repository/workflow.
- `401`: invalid/revoked token.
- `403`: token lacks Actions write permission.
- `422`: incorrect branch/ref or body.
