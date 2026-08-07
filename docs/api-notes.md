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

- **Chore ids are per-occurrence.** `chore.id` is `"<chore_id>-<date>"`. The spec
  says the underlying id is `chore.attributes.id`; live traffic says otherwise —
  see the corrections below.
- **`recurrence_set` holds RRULEs**, e.g.
  `"RRULE:FREQ=WEEKLY;INTERVAL=2;BYDAY=MO;WKST=SU"` (a list, not a string — see
  below).
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

## Verified against a live account

Every non-destructive (GET) call in pylight was run against a real Skylight
account — 36/36 succeed. That pass corrected both sources:

**Chore ids.** `attributes.id` is *not* the numeric chore id the spec implies; it
repeats the occurrence id (`"90317769-2026-08-02"`). The addressable id is the
`group` attribute, with `series` alongside it. `Chore.chore_id` reads `group`.

**`recurrence_set` is a list of RRULE strings**, not a single string. Calendar
events likewise carry `rrule` as a list.

**Profile pictures.** The spec's `profile_pic_url` does not exist. Categories
return `profile_picture_urls`, a dict of `small`/`medium`/`large`/`xl`/`original`.

**`/api/frames/calendar` and `/api/frames/photo` return collections**, not a
single frame — the resource type is `approved_viewer_frame`. Hence
`get_calendar_frames()` and `get_photo_frames()`, both plural.

**`/chores/all` is not a JSON:API document.** It returns
`{"chores": {...}, "routines": {...}}`, each bucketed into `late`, `today`,
`today_timed`, `any_day`, and `future`, each bucket its own `{data, included}`
document. Modeled as `ChoreGroups`.

**`/reward_points` is a plain JSON array** of
`{category_id, current_point_balance, lifetime_points_earned}` — no JSON:API
envelope, so `RewardPoint` is not an `ApiObject`.

**Four endpoints reject requests missing a parameter neither source lists as
required**, with a 422:

| Endpoint | Required | Error |
|---|---|---|
| `calendar_events/countdowns` | `timezone` | `Timezone is required` |
| `nudges` | `after` **and** `before` | `After is required` / `Before is required` |
| `meals/sittings` | `date_min`, `date_max` | `Date min is required` |

pylight makes all of these required arguments.

**Relationship names vary by endpoint.** `calendar_events` side-loads
`categories` (plural, to-many) while `countdowns` returns `category` (singular).
`CalendarEvent` exposes both `category_id` and `category_ids`.

**Attribute coverage** is much wider than either source documented — frames carry
36 attributes (sleep schedule, slideshow settings, feature bundle, share token),
devices 24 (including nightlight and sleep sound), chores 22, calendar events 23.
All are now modeled.

## Known gaps

The verification account had no nudges and no device alarms, so `Nudge` and
`Alarm` still carry only an id and raw attributes. Write endpoints (POST/PUT/
PATCH/DELETE) are implemented from the documented shapes but deliberately were
not exercised.
