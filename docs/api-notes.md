# API sources and where they disagree

pylight is built from two independent reverse-engineering efforts:

| Source | What it is | Strength |
|---|---|---|
| [mightybandito/Skylight](https://github.com/mightybandito/Skylight) — `docs/openapi/openapi.yaml`, v0.3.0 | An OpenAPI 3.0.3 spec plus redacted request/response captures | **Response shapes.** Concrete JSON:API attribute names, types, and nullability for the resources it covers |
| [Local notes gist](https://gist.github.com/dknowles2/b8eab833eb23eb388c3d78999a3565f8) | A hand-written endpoint reference derived from app traffic and client bundle symbols | **Breadth and auth.** ~150 endpoints across 15 areas, plus the full OAuth login flow and required headers |

They barely overlap, which is what makes them useful together: the spec has 12
paths with real schemas; the gist has the whole surface with almost no schemas.

## Coverage

The OpenAPI spec documents 12 paths, all read-only except two:

- `GET /api/frames/{frameId}`
- `GET|POST /api/frames/{frameId}/chores`
- `GET /api/frames/{frameId}/categories`
- `GET /api/frames/{frameId}/devices`
- `GET /api/frames/{frameId}/lists`, `GET .../lists/{listId}`
- `POST /api/frames/{frameId}/task_box/items`
- `GET /api/frames/{frameId}/source_calendars`
- `GET /api/frames/{frameId}/calendar_events`
- `GET /api/frames/{frameId}/rewards`, `GET .../reward_points`

The gist adds everything else: user/profile, frame lifecycle, device alarms and
resets, chore search/move/completions, task-box CRUD, calendar accounts and
WebCal, meals, nudges, photos/albums, rewards CRUD and redemption, Sidekick
(AI auto-creation), frame user access, Plus subscription, and utility endpoints.

## Where they conflict

**Authentication.** The OpenAPI spec declares two security schemes —
`Authorization: Basic <opaque token>` and `Authorization: Bearer <JWT>` — and its
`docs/auth.md` says to capture one by hand with a proxy. The gist documents the
actual flow: OAuth 2.0 authorization code + PKCE against `/oauth/authorize` and
`/oauth/token`, with credentials posted to a Rails login form at `/auth/session`.

*pylight follows the gist* and implements the flow end to end
([`pylight/auth.py`](../pylight/auth.py)). The `Basic` scheme in the spec is
almost certainly a legacy token seen in older captures — note the gist's
`POST /api/oauth/legacy_token_exchange`. `TokenAuth` covers that case: pass any
pre-obtained token and pylight sends it as a bearer token.

**Creating chores.** The spec documents `POST /api/frames/{frameId}/chores` with
a JSON:API single-resource body, and ships a captured example. The gist lists
only `POST /api/frames/{frameId}/chores/create_multiple`. Both are plausible —
the app likely uses the bulk endpoint while the singular one still exists.

*pylight exposes both*: `create_chore()` (JSON:API, matches the captured example)
and `create_chores()` (bulk).

**Task box.** The spec has only `POST .../task_box/items`; the gist adds `GET`,
`PATCH`, and `DELETE`. pylight implements all four, with the response typed from
the spec's `TaskBoxItemAttributes`.

**Headers.** The spec mentions none beyond `Authorization`. The gist records that
every app request sends `User-Agent: SkylightMobile (web)` and
`Skylight-Api-Version: 2026-05-01` alongside `Accept: application/json`. pylight
sends all four on every request; see [`pylight/const.py`](../pylight/const.py).

**Base URL.** Same host either way: the spec's server is
`https://app.ourskylight.com` with paths including `/api`; the gist quotes the
base as `https://app.ourskylight.com/api`.

**Spec version.** The repo's README claims OpenAPI 3.1; the file declares 3.0.3.
Cosmetic, but worth knowing if you tool against it.

## Schema details worth keeping

Facts only the OpenAPI spec and its examples record:

- **Chore ids are per-occurrence.** `chore.id` is `"<chore_id>-<date>"` while
  `chore.attributes.id` is the underlying integer chore id. pylight surfaces the
  latter as `Chore.chore_id`, which is what update/delete/completion calls need.
- **`recurrence_set` is an RRULE string**, e.g.
  `"RRULE:FREQ=WEEKLY;INTERVAL=2;BYDAY=MO;WKST=SU"`.
- **Colors are `#RRGGBB`**, though one captured example (`get-lists-listid.json`)
  shows a 7-character value with no `#`, so treat the leading `#` as optional.
- **`list.kind`** is `shopping` or `to_do`; **`list_item.status`** is `pending` or
  `completed`; **`chore.status`** is at least `pending`.
- **`GET .../lists/{listId}`** returns list items under `included` and sections
  under `meta.sections`. `SkylightList` from `get_list()` resolves both.
- **304 Not Modified** is documented on nearly every GET, so the API is
  conditional-request aware. pylight returns `None` (or an empty list) rather
  than treating it as an error; it does not yet send `If-None-Match`.
- **`calendar_events`** requires `date_min` and `date_max`, and accepts an
  `include` CSV of `categories,calendar_account,event_notification_setting`.
- **`chores`** accepts `after`, `before`, `include_late`, and
  `filter=linked_to_profile`.

Everything the spec marks `additionalProperties: true` — which is nearly every
resource — is preserved verbatim on each model as `.attributes`.

## Known gaps

No source has captured a body for frames, devices, calendar events, source
calendars, rewards, reward points, alarms, or nudges. Those models carry an id
and the raw attributes only; fill them in as captures appear.
