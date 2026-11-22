# Proactive chat and campaign onboarding

Apply `python -m alembic upgrade head` before starting the updated backend. Revision
`0061_proactivity` adds the question catalog, deduplication records and persisted
message read timestamps. Existing messages become read on upgrade. Attribution is
stored inside the already encrypted user profile, so existing encryption and key
rotation tools continue to cover it.

## Dashboard

`/dashboard/proactivity` lets administrators create, edit, disable and delete
questions, set their order, and configure up to six follow-up tool prompts in each
language. A blank prompt list uses the built-in calendar, task, reminder and team
entry points. The four initial general questions are examples from the task;
campaign-specific questions are entered by administrators.

Target a question with an exact `group_id` matching `utm_group`, an optional
scenario (`study`, `work`, `personal`, `day`), or neither for a general question.
Questions must match every configured condition. Exact groups have priority over
scenario-only questions, which have priority over general questions. Questions
are delivered once per account. Disabling a question stops future delivery.

After an affirmative answer to an opening question, Deyla offers the configured
tool prompts as chat buttons. Choosing one sends it through the ordinary assistant
pipeline. A negative answer leaves the user free to return later. Specific task
requests bypass this short dialogue and continue through the normal assistant.

## Attribution

The browser captures `utm_source`, `utm_medium`, `utm_campaign`, `utm_group`,
`utm_content`, and `utm_term` before navigation. Local storage preserves the first
and latest touch, their timestamps and advertising URLs. OAuth codes, tokens,
unrelated query parameters and fragments are excluded from the saved URL.

Registration and signed Google/Yandex OAuth state carry attribution to the account.
`PUT /api/me/attribution` attaches subsequent visits after authentication. The first
server-recorded touch is immutable; older browser visits cannot overwrite the
latest touch. Visits without UTM parameters preserve both touches.

For Yandex, keyword stems map study, work, personal and day-planning campaigns to
the corresponding scenario. Unknown keywords and Telegram Ads use the general
scenario. An explicit group ID can still select an administrator-defined question.

The dashboard's virtual `campaign_attribution` sheet exposes first/latest campaign
fields and dates by `user_id`. Combine it with `users`, `product_activity`, or
other action tables to analyze registrations and activity by campaign. It does not
expose unrelated encrypted profile contents or full landing URLs.

## Suggestions and unread messages

A background scan runs while the FastAPI application is running. The assistant
inbox also refreshes suggestions when a user is active. Delivery keys and per-user
database locks prevent duplicate messages across workers and devices. There are
at most three proactive messages per local day, including the welcome; further
dialogue questions arrive at most daily between 08:00 and 21:00 local time.

Timed presentations/reports can receive a suggested 20-minute rehearsal in a
verified free window before the event. Untimed and cancelled tasks stay untimed
and are excluded. Repeated task pairs on at least three distinct days with
consistent timing produce follow-up suggestions. The explicit saved preference
about shopping 30 minutes after a run starts is also supported before sufficient
history exists. The proposed shopping task starts at minute 30 and has a
five-minute lead reminder, i.e. minute 25 after the run starts.

Offers are saved as assistant messages, without creating events. The owner may
dismiss them or open a regular task draft. Opening and final confirmation both
recheck ownership, the source event, the user's timezone, elapsed time and calendar
conflicts. Opening an offer cannot replace an existing unfinished draft. A user
still confirms calendar changes, and friend/team sending keeps its existing
confirmation flow. Explicit reminder prompts keep the existing immediate path.

`GET /api/assistant/inbox?after=<message id>` returns messages and the unread count.
`POST /api/assistant/read` accepts `through_id` and marks only that account's visible
assistant messages up to that ID read. Desktop navigation, the chat launcher and
mobile navigation show the unread count. The chat marks messages read only while
the document is visible and focused. Read state persists across reloads/devices.

The worker stores messages in the shared assistant history; it does not directly
send Telegram messages or make model/provider calls. Confirmed task reminders
continue through the existing notification system.

Downgrading revision 0061 removes the question catalog, delivery history and read
timestamps. Campaign attribution remains in the encrypted profile. Do not
downgrade a production database as a validation step.
