# Focus Day API

Backend for Focus Day, a calendar assistant built on FastAPI. It stores users, calendars and events in PostgreSQL. Users talk to it through a GigaChat-powered assistant in text, voice or uploaded documents, and it syncs events with Google Calendar.

## Features

- **Events and calendars**: CRUD for events, including reminders and recurrence, plus file attachments (PDF, DOC/DOCX, XLS/XLSX, PNG/JPG).
- **AI assistant**: natural-language requests (in Russian) are parsed by GigaChat into proposed events, which the user confirms before they are saved.
- **Assistant agent** (`app/services/agent.py`): GigaChat gets the user's onboarding profile (spheres and goals by priority, tone of voice, work days and hours), habits and the plan for the next 7 days, and calls the app's functions: find, add, change, move, delete and complete tasks, set reminders, analyse the plan, split a big task into steps. The app runs each function and returns the real result to the model. Changes go into one draft the user confirms; completing tasks and reminders apply at once. Built to work with the free GigaChat Lite: tasks get short per-turn numbers, days and times are resolved from the user's own words by `app/services/dates.py`, the chosen task is checked against the words of the request, and confirmations are written by the app, not by the model. A clear command the model only talked about is done by the rule-based handlers; when GigaChat is unreachable they answer alone (`ASSISTANT_AGENT=false` turns the agent off). The model behind it is chosen with `LLM_PROVIDER`: GigaChat, or any OpenAI-compatible API (`API_KEY`, `BASE_URL`, `OPENAI_MODEL`); the agent's functions are sent to it as OpenAI tools.
- **Reminders on request**: "напомни помыть посуду через 10 минут", "напомни завтра в 9 позвонить в банк" — the bot sends the message at that time (also in quiet hours, since the user chose it), with snooze buttons. "Какие у меня напоминания?", "отмени напоминание про посуду", or "Отменить" under the answer (`DELETE /api/assistant/reminders/{id}`). They need Telegram to be connected.
- **Voice input**: audio is converted with ffmpeg and transcribed through Google Speech Recognition (`ru-RU`).
- **Document input**: text is extracted from PDF/DOCX files and passed to the assistant.
- **Google Calendar**: OAuth login, two-way sync of events and calendars.
- **Export**: `.ics` export of the user's calendar.
- **Auth**: email/password registration with Argon2 hashing. Short-lived JWT access tokens plus refresh tokens, sent either as `Authorization: Bearer` or as httpOnly cookies. Every `/api` route is scoped to the current user.
- **Integrations**: Google Calendar (OAuth), Apple Calendar (iCloud/any CalDAV, app-specific password), Jira Cloud (email + API token), Notion (internal integration token + database). Each one supports test, import (sync) into the local calendar, and export of local events. Credentials are Fernet-encrypted.
- **Tasks**: a task without a stated time is stored as an untimed (all-day) task instead of getting a default time. Recurring tasks ("каждую пятницу в 18:00", "по будням") become a series that is kept filled 90 days ahead. Tasks can be marked completed; `/api/stats` returns daily completion statistics.
- **Dates**: `app/services/dates.py` resolves Russian date, time and recurrence phrases ("в следующую пятницу", "через два дня", "по вторникам") with `datetime`. GigaChat only reports the phrases it found for each event.
- **Confirmation stage**: the assistant never creates events right away. It saves a draft that the user can edit (title, date, time, remove items) and then confirms or cancels. This works on the site and in the bot.
- **Midday check-in and recommendations**: the onboarding answers (`PUT /api/me/onboarding`: work days and hours, goals, tone) are used for the midday check-in in Telegram, which suggests tasks to move to a less busy day, and for the two AI recommendations on the main screen.
- **Reminders**: per-user settings (lead times, per-event override, daily digest, quiet hours, source filter). The backend queues notifications in an outbox and the Telegram bot delivers them.
- **Changing tasks in the chat**: "перенеси созвон с Олей на пятницу в 15:00", "продли встречу до 18:00", "сдвинь тренировку на час позже", "переименуй … в …". The assistant finds the task and shows the change (before → after) as a draft; nothing changes until it is confirmed. Multi-day tasks ("с 10 по 12 октября") keep their last day (`end_date` in drafts).
- **Deleting, completing and analysing in the chat**: "удали все события / встречу с Олей / задачи на завтра" shows exactly what will be removed and deletes only after confirmation; "отметь отчёт выполненным", "я сделала отчёт" mark tasks done; "проанализируй мою неделю", "что можно перенести?" return an analysis plus moves of flexible tasks to confirm. A model reply that claims a change the app did not make ("удалила…") is replaced with an honest one.
- **Answer ratings**: 👍 / 👎 under assistant answers (site and bot), stored in `conversation_messages.rating`. `python -m app.core.ratings_export > ratings.csv` exports rated answers with the requests before them (decrypted — handle as personal data).
- **Tags, deadlines, fixed tasks**: `/api/tags` (create, rename, recolor, delete); events carry `tag_ids`, `deadline_at` and `is_fixed` ("нельзя переносить"). `#тег` in a chat message tags the new tasks. Deadline notifications go out 3 days, 1 day and 2 hours before.
- **Evening summary and streaks**: at the evening time (default 21:00) the bot sends the day's progress, unfinished tasks with a button to move them to tomorrow, and tomorrow's plan. `/api/stats` returns the current and best streak of fully completed days (days without tasks do not break it).
- **Calendar advice**: `GET /api/recommendations?scope=week|month&day=` analyses the period: busy and free days, deadlines and tasks that cannot be moved, and suggests where to put flexible tasks. `POST /api/assistant/topic` opens the chat about a recommendation.

## Tech stack

Python 3.12 · FastAPI · SQLAlchemy 2 (async) + asyncpg · Alembic · PostgreSQL 16 · GigaChat API · Google Calendar API · Docker Compose

## Project structure

```
app/
  main.py               # FastAPI app, /health
  api/routes.py         # all API routes
  core/                 # config, DB session, JWT/password helpers
  models/models.py      # SQLAlchemy models
  schemas.py            # Pydantic schemas
  services/             # calendar provider abstraction, Google Calendar client
services/
  gigachat.py           # GigaChat client and date/intent parsing
  speech.py             # audio → text
  text_extractors.py    # PDF/DOCX → text
  calendar.py           # calendar helpers
alembic/                # database migrations
storage/                # uploaded files (runtime, not committed)
```

## Configuration

Copy `.env.example` to `.env` and fill in the values:

| Variable | Description |
| --- | --- |
| `APP_ENV` | `production` refuses to start with weak secrets, `COOKIE_SECURE=false` or non-HTTPS `CORS_ORIGINS`, and hides `/docs`. Default `development`; the Docker image sets `production` |
| `POSTGRES_DB`, `POSTGRES_USER`, `POSTGRES_PASSWORD` | Postgres credentials (used by Docker Compose) |
| `DATABASE_URL` | SQLAlchemy async URL, e.g. `postgresql+asyncpg://user:pass@host:5432/db` (`postgres://` is accepted too) |
| `SECRET_KEY` | Secret for signing JWTs and the OAuth state, at least 32 random characters |
| `JWT_ALGORITHM`, `JWT_EXPIRE_MINUTES`, `JWT_REFRESH_EXPIRE_DAYS` | JWT settings (default `HS256`, `15`, `30`). Refresh tokens rotate on each use and are revoked by `/auth/logout` and `/auth/logout-all` |
| `COOKIE_SECURE` | Mark auth cookies `Secure` (set `true` behind HTTPS) |
| `CORS_ORIGINS` | Comma-separated extra browser origins allowed to call the API. Empty (default) means same-origin only. `*` is rejected |
| `ENABLE_DOCS` | Swagger UI and `/openapi.json` (default: on in development, off in production) |
| `ALLOW_PRIVATE_INTEGRATION_URLS` | Let Jira and CalDAV URLs point to private or loopback addresses (default: on in development, off in production) |
| `GIGACHAT_CA_BUNDLE` | PEM file with the Russian Trusted Root CA, used to verify GigaChat's TLS certificate |
| `DATA_ENCRYPTION_KEY` | AES-256 key (32 bytes, base64url) for personal data in the database. **Back it up**: without it the data cannot be read. If empty, a key derived from `SECRET_KEY` is used |
| `DATA_ENCRYPTION_OLD_KEYS` | Comma-separated previous keys, still used for decryption after a rotation |
| `INTEGRATIONS_ENCRYPTION_KEY` | Fernet key for integration credentials; derived from `SECRET_KEY` if empty |
| `BOT_API_TOKEN` | Shared secret for the bot's `/internal/bot/*` API (same value in `tg_bot/.env`) |
| `TELEGRAM_BOT_USERNAME` | Bot username, used for the `t.me/<bot>?start=<code>` linking link |
| `PUBLIC_APP_URL` | Public `https://` address of the site; the bot links events to it with "Открыть в Dayla" buttons (hidden when empty) |
| `DEFAULT_TIMEZONE` | Fallback user timezone (default `Europe/Moscow`) |
| `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET` | Google OAuth client |
| `GOOGLE_REDIRECT_URI` | OAuth callback, must end with `/auth/google/callback` |
| `YANDEX_CLIENT_ID`, `YANDEX_CLIENT_SECRET` | Yandex ID OAuth app (oauth.yandex.ru) with the «Яндекс Календарь: calendar:all» permission |
| `YANDEX_REDIRECT_URI` | OAuth callback, must end with `/auth/yandex/callback` and match the app's Redirect URI |
| `LLM_PROVIDER` | Which service answers every model request, including the agent: `gigachat` (default) or `openai` — any OpenAI-compatible API |
| `API_KEY` | Key of the OpenAI-compatible API (`LLM_PROVIDER=openai`) |
| `BASE_URL` | Its address with `/v1`, e.g. `https://shared1.multitool.works:4000/v1` |
| `OPENAI_MODEL` | Model name at `BASE_URL` (see `BASE_URL/models`); required with `LLM_PROVIDER=openai` |
| `OPENAI_AGENT_MODEL` | Model of the assistant agent (default: `OPENAI_MODEL`) |
| `OPENAI_VISION_MODEL` | Model that reads photos sent to the bot (tickets, screenshots); default `OPENAI_MODEL` |
| `OPENAI_THINKING` | Reasoning of models like DeepSeek V4: `off`, `low` or `high` (more room is given for it); empty sends nothing, for APIs without these parameters. DeepSeek: `BASE_URL=https://api.deepseek.com/v1`, `OPENAI_AGENT_MODEL=deepseek-flash`, `OPENAI_MODEL=deepseek-v4-pro`, `OPENAI_VISION_MODEL=deepseek-flash`, `OPENAI_THINKING=low` |
| `SBER_AUTHORIZATION_KEY` | GigaChat authorization key |
| `SBER_SCOPE` | GigaChat scope (default `GIGACHAT_API_PERS`) |
| `GIGACHAT_MODEL` | GigaChat model name (default `GigaChat`) |
| `GIGACHAT_AGENT_MODEL` | Model of the assistant agent (default: `GIGACHAT_MODEL`); `GigaChat-2-Pro` picks functions more reliably than the free Lite |
| `ASSISTANT_AGENT` | `false` turns the agent off: the rule-based assistant answers alone (default `true`) |
| `STORAGE_PATH` | Directory for uploaded files (default `./storage`) |
| `MAX_FILE_SIZE_MB` | Upload size limit (default `25`) |

## Running

### Whole stack (backend + Telegram bot + Postgres)

From the repository root:

```bash
docker compose up -d --build
```

Only the backend uses the database and GigaChat; the bot calls the backend's `/internal/bot/*` API and starts after the backend is healthy. `BOT_API_TOKEN` and the URLs between services are set in the root `docker-compose.yml`. The bot's username for the linking link comes from the bot token (`getMe`) unless `TELEGRAM_BOT_USERNAME` is set.

### Backend only (Docker Compose)

```bash
cp .env.example .env   # then edit it
docker compose up -d --build
```

The API listens on `http://127.0.0.1:8000`. On start, the container runs `alembic upgrade head` before it launches uvicorn.

### Locally

Requires Python 3.12, PostgreSQL, and ffmpeg (the `imageio-ffmpeg` package provides a bundled binary).

```bash
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # point DATABASE_URL at your local Postgres
alembic upgrade head
uvicorn app.main:app --reload
```

## API overview

Interactive docs are served at `/docs` (Swagger) and `/redoc`.

| Area | Endpoints |
| --- | --- |
| Health | `GET /health` |
| Auth | `POST /auth/register`, `POST /auth/login`, `POST /auth/token` (OAuth2 form, for Swagger), `POST /auth/refresh`, `POST /auth/logout` |
| Google OAuth | `GET /auth/google/login`, `GET /auth/google/callback`, `POST /auth/google/disconnect` |
| Profile | `GET/PATCH /api/me`, `PUT /api/me/onboarding` |
| Calendars | `GET/POST /api/calendars`, `GET /api/calendars/{id}`, `POST /api/calendars/{id}/sync` |
| Events | `GET/POST /api/events` (`recurrence_rule` creates a series), `GET/PUT/DELETE /api/events/{id}` (`?scope=series` deletes this and later occurrences), `POST /api/events/{id}/complete`, `POST /api/events/move`, `POST /api/events/{id}/sync/google`, `GET /api/events/{id}/links` |
| Statistics | `GET /api/stats?days=7`, `GET /api/recommendations` |
| Integrations | `GET /api/integrations`, `POST /api/integrations/{provider}/connect`, `.../test`, `.../sync`, `POST /api/integrations/{provider}/export/{event_id}`, `DELETE /api/integrations/{provider}?purge=` |
| Reminders | `GET/PUT /api/reminders/settings`, `POST /api/reminders/test`, `GET /api/reminders/history` |
| Telegram | `GET/DELETE /api/telegram`, `POST /api/telegram/link` |
| Bot (internal, `X-Bot-Token`) | `/internal/bot/link`, `/unlink/{chat_id}`, `/users/{chat_id}/reminder-settings`, `/chat/{chat_id}` (message → proposal, agenda or answer), `/chat/{chat_id}/drafts/{id}` (+ `/edit`, `/remove`, `/confirm`, `/cancel`), `/chat/{chat_id}/events/{id}/complete`, `/chat/{chat_id}/stats`, `/notifications/claim`, `/notifications/{id}/ack`, `/notifications/{id}/snooze`, `/notifications/{id}/checkin` |
| Files | `POST/GET /api/events/{id}/files`, `DELETE /api/files/{id}`, `POST /api/files/{id}/text` |
| Assistant | `POST /api/assistant/chat` (proposal → `PUT /api/assistant/drafts/{id}`, `POST .../confirm`, `DELETE`), `POST /api/assistant/search`, `POST /api/assistant/transcribe`, `POST /api/assistant/file`; legacy `POST /api/assistant/message`, `POST /api/assistant/confirm` |
| Export | `GET /api/calendar/export.ics` |

### Reminder flow

1. On the site the user clicks "Подключить Telegram" and gets a deep link `t.me/<bot>?start=<code>` (single-use, valid for 15 minutes).
2. The bot receives `/start <code>` and calls `/internal/bot/link`, which stores the chat id on the user.
3. Every `NOTIFICATION_POLL_SECONDS` the bot calls `/notifications/claim`. That call extends recurring series, queues due reminders, digests and the midday check-in (deduplicated by a key), skips quiet hours, expires stale items, and hands the batch to the bot (`FOR UPDATE SKIP LOCKED`, so several bot instances are safe).
4. The bot sends each message and acks it. Transient errors are retried up to 3 times. If the user blocked the bot, Telegram is unlinked.

## Data encryption

Personal data is encrypted in the application before it reaches PostgreSQL (AES-256-GCM, `app/core/dataenc.py`), so a database dump or leaked backup shows only ciphertext:

- users: email, name, Telegram username, onboarding profile (login uses `email_hash`, an HMAC of the email);
- tasks: title, description, location; calendar names; file names; integration account emails;
- assistant history and drafts; reminder and check-in texts.

Dates and times, ids and statuses stay readable, because the calendar, reminders and statistics filter and sort by them. Passwords are Argon2 hashes; integration credentials are encrypted with `INTEGRATIONS_ENCRYPTION_KEY`.

The key is read from the environment and never stored in the database. Migration `0023` encrypts existing rows. To set or rotate the key: put the new key in `DATA_ENCRYPTION_KEY`, keep the previous one in `DATA_ENCRYPTION_OLD_KEYS` (not needed when moving from the `SECRET_KEY`-derived key), then run `python -m app.core.reencrypt`. Until `DATA_ENCRYPTION_KEY` is set, **do not change `SECRET_KEY`**: the data key is derived from it.

Encryption at rest does not protect against a compromised server (the server holds the key) or data already sent to GigaChat, Google Calendar or Telegram.

## Tests

See `tests/README.md`: `tests/test_dates.py` runs without a database, the rest need PostgreSQL migrated to head.

## Migrations

```bash
alembic revision -m "description"   # create a new migration in alembic/versions
alembic upgrade head                # apply all migrations
```
