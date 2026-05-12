# Google Calendar & Tasks API — LLM reference

Dense patterns for constructing REST requests. **Calendar events** and **Tasks** use **different APIs**, scopes, and payloads—do not mix them.

---

## 1. APIs at a glance

| Goal | API | Base (REST) |
|------|-----|-------------|
| Meetings, blocks, recurring meetings on a calendar | **Calendar API v3** | `https://www.googleapis.com/calendar/v3` |
| To-do items (checkbox lists, not calendar blocks by default) | **Tasks API v1** | `https://tasks.googleapis.com/tasks/v1` |

Authentication: OAuth 2.0 **access token** (or equivalent) in header:

```http
Authorization: Bearer <ACCESS_TOKEN>
```

Typical scopes:

- Calendar (read/write events): `https://www.googleapis.com/auth/calendar.events` or broader `https://www.googleapis.com/auth/calendar`
- Tasks: `https://www.googleapis.com/auth/tasks`

---

## 2. Calendar API — identify calendar + endpoint

- **Primary calendar** id is usually the user’s email address (e.g. `user@gmail.com`).
- **Secondary calendars**: list with `GET /users/me/calendarList` and use each entry’s `id`.

**Create event** (single insert):

```http
POST https://www.googleapis.com/calendar/v3/calendars/{calendarId}/events
```

Optional query flags you should know:

- `sendUpdates=all|externalOnly|none` — whether to email attendees on create/update.

---

## 3. Single timed calendar event

Use **`start.dateTime`** + **`end.dateTime`** with **`timeZone`** — **IANA name only**
(e.g. `America/Los_Angeles`, `America/Chicago`). **Do not** use abbreviation-only zones like **`CDT`**
or **`PST`** as `timeZone`; map from the airport / city appendix when available.

Minimal JSON body pattern:

```json
{
  "summary": "Short title shown in Calendar UI",
  "description": "Optional longer notes, URLs, agenda.",
  "location": "Optional physical or Meet link text",
  "start": {
    "dateTime": "2026-05-10T14:00:00",
    "timeZone": "America/Los_Angeles"
  },
  "end": {
    "dateTime": "2026-05-10T15:00:00",
    "timeZone": "America/Los_Angeles"
  },
  "attendees": [
    { "email": "alice@example.com" },
    { "email": "bob@example.com", "optional": true }
  ],
  "reminders": {
    "useDefault": false,
    "overrides": [
      { "method": "popup", "minutes": 10 },
      { "method": "email", "minutes": 60 }
    ]
  }
}
```

Rules:

- **`end` must be after `start`**.
- If you omit custom reminders and set `"useDefault": true`, calendar default reminders apply.

---

## 4. Single all-day calendar event

Use **`start.date`** and **`end.date`** as **exclusive** end dates (RFC3339 **date only**).

Example: one full day **May 10**:

```json
{
  "summary": "All-day off",
  "start": { "date": "2026-05-10" },
  "end": { "date": "2026-05-11" }
}
```

Multi-day all-day spanning May 10–12 (inclusive):

```json
"start": { "date": "2026-05-10" },
"end": { "date": "2026-05-13" }
```

Never mix `date` on one side and `dateTime` on the other for the same event.

---

## 5. Recurring (repeating) calendar events

Put recurrence rules in **`recurrence`**: an array of **RFC 5545** strings (`RRULE`, optional `EXDATE`, `RDATE`).

Most common: **`RRULE`** only.

Examples:

| Intent | `recurrence` entry |
|--------|-------------------|
| Daily forever | `"RRULE:FREQ=DAILY"` |
| Every week on Mo/We | `"RRULE:FREQ=WEEKLY;BYDAY=MO,WE"` |
| Monthly on the 15th | `"RRULE:FREQ=MONTHLY;BYMONTHDAY=15"` |
| Yearly | `"RRULE:FREQ=YEARLY"` |
| End after 10 occurrences | `"RRULE:FREQ=WEEKLY;COUNT=10"` |
| End by date (UTC date in rule) | `"RRULE:FREQ=WEEKLY;UNTIL=20261231T235959Z"` |

Sample repeating meeting:

```json
{
  "summary": "Weekly sync",
  "start": {
    "dateTime": "2026-05-12T10:00:00",
    "timeZone": "UTC"
  },
  "end": {
    "dateTime": "2026-05-12T10:30:00",
    "timeZone": "UTC"
  },
  "recurrence": [
    "RRULE:FREQ=WEEKLY;BYDAY=TU;COUNT=12"
  ]
}
```

Important LLM rules:

- **`UNTIL` vs `COUNT`**: prefer one; combining needs care.
- **`BYDAY`**: `MO,TU,WE,TH,FR,SA,SU`.
- **Exceptions**: changing one instance is done via **instances API** or **`PATCH`** on that instance’s event id—do not assume editing the master copies all exceptions (Clients often create `recurringEventId` relationships).

---

## 6. Calendar — update, delete, list (minimal map)

| Action | Method | Path pattern |
|--------|--------|----------------|
| List events in range | `GET` | `/calendars/{calendarId}/events?timeMin=...&timeMin=...` (ISO8601, often Zulu) |
| Get one | `GET` | `/calendars/{calendarId}/events/{eventId}` |
| Patch fields | `PATCH` | same path, partial JSON body |
| Replace entire resource | `PUT` | same path, full body |
| Delete | `DELETE` | same path |
| Quick add natural language | `POST` | `/calendars/{calendarId}/events/quickAdd` with `text` query param |

Use **`singleEvents=true`** when expanding recurring masters into occurrences in listings.

---

## 7. Tasks API — “tasks” are not calendar events

Tasks live in **task lists**. Flow:

1. `GET https://tasks.googleapis.com/tasks/v1/users/@me/lists` → obtain `id` of a list (default list has an id).
2. Create task on that list:

```http
POST https://tasks.googleapis.com/tasks/v1/lists/{taskListId}/tasks
```

Typical JSON:

```json
{
  "title": "Buy milk",
  "notes": "Optional details",
  "due": "2026-05-10T00:00:00.000Z"
}
```

Notes:

- **`due`** is RFC3339; many clients treat tasks as **due on a date**—using midnight UTC for date-only intent is a common pattern.
- **`completed`**: set `true` and usually provide `completed` timestamp when marking done (client libraries vary).
- **No native recurrence** in Tasks API like Calendar `RRULE`; repeating tasks are often modeled as **one task + app logic** or **Calendar recurring events** instead.

Subtasks: optional **`parent`** field pointing to another task’s id (same list), depending on client/feature availability—verify in current API docs if you rely on hierarchy.

---

## 8. Structured checklist for an LLM before emitting a request

1. **Choose API**: time-block on a calendar → Calendar; checklist item → Tasks.
2. **Calendar timed vs all-day**: `dateTime`+`timeZone` vs `date` + exclusive `end.date`.
3. **Recurring?** → add `recurrence` with valid `RRULE` strings only.
4. **Attendees?** → `attendees[]` + decide `sendUpdates`.
5. **Which calendar / list?** → correct `{calendarId}` or `{taskListId}`.
6. **Idempotency**: inserts create **new** resources; updates need **`eventId`** / **`task` id** from prior responses.

---

## 9. Common mistakes to avoid

- Using **Tasks** endpoint JSON (`title`, `notes`) against **Calendar** `/events` — schemas differ.
- All-day **`end.date`** not exclusive → wrong duration.
- **`RRULE`** time zone surprises: master `start` anchors the series; complex TZ/DST edge cases may need human verification.
- Assuming **`PATCH`** merges nested objects exactly like flat keys—send only fields to change when possible.

Keep this file alongside tool schemas or function definitions so the model maps **user intent → correct API + minimal JSON body**.
