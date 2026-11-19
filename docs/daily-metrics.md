# Daily product metrics export

GET `/api/v1/metrics/daily?date_from=2026-11-08&date_to=2026-11-14`

Header: `Authorization: Bearer <dedicated-token>`.

Both dates are required, strictly `YYYY-MM-DD`, inclusive, and may span at most
90 calendar days. Fetch longer history in consecutive chunks of at most 90 days.
Dates are supplied by the collector; no marketing start date is hardcoded.

The response contains exactly `startup_id`, `timezone`, `generated_at`, and `data`.
Every daily record contains `date`, `dau`, `llm_requests`, `tool_requests`,
`total_users`, `new_users`, and `is_final`. Counters are JSON integers. Days are
Moscow midnight to the next midnight (UTC+3). `generated_at` includes `+03:00`.
`is_final` means the calendar day is over; today and future dates are not final.
It does not certify historical coverage or prevent an operator changing exclusions.

Every requested date is returned. Inactive days have zero activity/new-user
counters; their accumulated `total_users` is carried forward, not reset to zero.
Only aggregates are returned; no names, contacts, account IDs or message text.

## Definitions and coverage

| Field | Calculation |
| --- | --- |
| `dau` | Distinct registered users with a successful explicitly targeted product request, a completed server agent function, or a user-initiated successful model response that day. Opening pages, listing records, login, background sync and notification polling do not count. |
| `llm_requests` | Successful completion responses recorded by the existing GigaChat/OpenAI-compatible adapter during targeted user requests. Each completion is separate, including multi-step agent completions. Failed attempts, token refreshes, embeddings and background model calls are excluded. |
| `tool_requests` | Successfully completed server assistant function invocations. Deyla exposes `act`: one invocation is one function call even when it contains several operations. Calls containing an error/rejection do not count. Creating a confirmable draft is a completed function; executing the later confirmation by a button is not another AI tool call. |
| `total_users` | Registrations before the end of the date, from a ledger seeded with existing accounts at migration. Subsequent account deletion preserves the registration count. |
| `new_users` | Registrations within the date. |

`Tool calls` is not exported: the collector can add `llm_requests + tool_requests`.

**Account policy requested by the product owner:** include everyone, including
administrators, team/test accounts, bots and autotests. This deliberately differs
from the TOR. `METRICS_EXCLUDED_USER_IDS` is empty by default. If the policy changes,
operators may specify comma-separated internal IDs; the same filter applies to
every counter and historical date. No IDs are returned by the endpoint.

The target route allowlist is in `app/services/product_metrics.py`. It covers
calendar creation/import/export/feed-link creation, event creation/update/
completion/uncompletion/move/Google sync/file upload, tag creation, assistant
messages/confirmation/undo/reminder cancellation/rating/question answers/draft
editing/cancellation/voice/file/search, reminder settings/test, Telegram linking,
and integration connect/test/sync/export. Telegram acts on behalf of the linked
human account; transport through a bot does not make that account a bot account.
Desktop model steps contribute to DAU and LLM counts.

Coverage limits that must be disclosed when connecting:

- Before migration `0060`, DAU is a lower-bound reconstruction from retained
  non-imported event creation, tag creation, user conversation messages and
  retained attributed model usage. Missing/deleted actions cannot be recovered.
  After migration only explicit request/function/model instrumentation is used;
  recurring background event generation cannot create activity through a proxy.
- Historical tool calls were not recorded. Their exported value is zero before
  tracking begins, meaning unavailable history rather than proof of no tools.
- Historical model rows lack a user-initiation marker. Attributed rows are
  included except `recommendations` and `plan_recommendations`; older deleted
  users without attribution cannot be recovered. New rows mark user initiation
  explicitly and preserve the metrics attribution across account deletion.
- Users deleted before migration are absent from the initial registration ledger.
- Desktop tools execute on the client. There is no server-verified, deduplicated
  completion receipt, so they are not included in `tool_requests`. Merely returning
  a proposed desktop action does not count as completion.
- Anonymous landing-page contact/search/messenger goals and the listed AI-source
  add/update/reparse goals have no corresponding instrumented backend flow in this
  checkout. They do not create invented user IDs or fabricate DAU.

Suggested one-line disclosure:

> All accounts included by owner request; historical DAU/LLM coverage is partial,
> tool counts start at deployment and cover server assistant functions only.

## Configuration and operations

1. Apply Alembic revision `0060_product_activity` before running the new backend.
   It creates content-free activity, registration and collection-start tables,
   adds initiation/metrics-attribution fields to `llm_usage`, and backfills existing
   registration/usage metadata. It never fabricates historical tool activity.
2. Set `METRICS_STARTUP_ID=deyla` or the slug assigned by the organizer.
3. Generate `METRICS_TOKEN` in the deployment secret manager: a separate random
   token of at least 32 characters (e.g. 32 random bytes encoded URL-safe).
   Leave it empty to disable export. Never reuse session, signing, bot or provider
   credentials. It has no automatic expiry. Revoke by replacing/removing it and
   restarting every backend worker; transfer the replacement securely.
4. Keep `METRICS_EXCLUDED_USER_IDS=` for the requested include-everyone policy.
5. Serve through the existing HTTPS ingress with TLS 1.2+. Configure Uvicorn to
   trust forwarded scheme headers only from the actual ingress. Production rejects
   requests whose trusted ASGI scheme is HTTP. TLS termination itself belongs to
   the hosting ingress and was not changed or probed by this implementation.
6. Configure ingress/access logs to redact `Authorization` and avoid response
   caching. Responses include `Cache-Control: no-store`.

The token grants access only to this read method; JWT/cookie/bot authentication
does not substitute for it. This route reads a consistent, read-only database
snapshot and uses an 8-second statement timeout and 9-second overall budget.
Errors are `{"error":"description"}`: 400 for invalid/missing/duplicate dates or
over-90-day/reversed ranges; 401 for missing/invalid/disabled credentials; 429 for
rate limiting; sanitized 503 for database failure/timeout. The limit is 30 requests
per minute per backend process/token. Shared limiting across multiple replicas
requires the existing infrastructure/ingress limiter. Collector retries on 5xx
(three attempts at 15-minute intervals) belong to the collector.

No scheduler or external sending job is needed: the collector downloads the
requested daily aggregates through GET. New activity records are written as actions
complete. Existing accounting intentionally logs a sanitized failure instead of
breaking the user's action if the activity sink is unavailable; monitor
`Product activity recording failed` for coverage gaps.

## Activation form preparation

The TOR mentions an organizer's activation form but supplies no URL or fields.
No form was submitted. Prepare the following for its actual fields:

| Item | Value |
| --- | --- |
| Startup ID | `deyla` (confirm assigned slug) |
| Endpoint | `https://<actual-product-domain>/api/v1/metrics/daily` |
| Authentication | Dedicated bearer token, delivered privately |
| Timezone | `Europe/Moscow` |
| Range | Inclusive `date_from`, `date_to`; up to 90 days |
| Marketing start date | Supplied by owner/organizer at connection |
| Definition difference | Use the one-line disclosure above |

Before activation, verify the last 30 days on the deployed HTTPS endpoint and
download from the actual marketing start date in chunks. Local checks do not
establish production history completeness, deployed TLS behavior or production
response times. No production migration, secret configuration or deployment was
performed as part of the code change.

Downgrading removes the new metrics history and usage-attribution fields; it
cannot reconstruct that history on upgrade. It leaves product accounts and
existing usage counters intact. Back up/retain metrics if an operational rollback
is required; no production downgrade was tested.
