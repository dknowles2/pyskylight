# API sources and where they disagree

pyskylight is built from two independent reverse-engineering efforts:

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

*pyskylight follows the gist* and implements the flow end to end
([`pyskylight/auth.py`](../pyskylight/auth.py)). The `Basic` scheme in the spec is
almost certainly a legacy token seen in older captures — note the gist's
`POST /api/oauth/legacy_token_exchange`. `TokenAuth` covers that case: pass any
pre-obtained token and pyskylight sends it as a bearer token.

**Creating chores.** The spec documents `POST /api/frames/{frameId}/chores` with
a JSON:API single-resource body, and ships a captured example. The gist lists
only `POST /api/frames/{frameId}/chores/create_multiple`. Both are plausible —
the app likely uses the bulk endpoint while the singular one still exists.

*pyskylight exposes both*: `create_chore()` (JSON:API, matches the captured example)
and `create_chores()` (bulk).

**Task box.** The spec has only `POST .../task_box/items`; the gist adds `GET`,
`PATCH`, and `DELETE`. pyskylight implements all four, with the response typed from
the spec's `TaskBoxItemAttributes`.

**Headers.** The spec mentions none beyond `Authorization`. The gist records that
every app request sends `User-Agent: SkylightMobile (web)` and
`Skylight-Api-Version: 2026-05-01` alongside `Accept: application/json`. pyskylight
sends all four on every request; see [`pyskylight/const.py`](../pyskylight/const.py).

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
  conditional-request aware. pyskylight returns `None` (or an empty list) rather
  than treating it as an error; it does not yet send `If-None-Match`.
- **`calendar_events`** requires `date_min` and `date_max`, and accepts an
  `include` CSV of `categories,calendar_account,event_notification_setting`.
- **`chores`** accepts `after`, `before`, `include_late`, and
  `filter=linked_to_profile`.

Everything the spec marks `additionalProperties: true` — which is nearly every
resource — is preserved verbatim on each model as `.attributes`.

## Error bodies come in two shapes

Undocumented, and worth knowing because half of them are useless without the
field name. A complaint about the request as a whole arrives as a list of
sentences:

```json
{"errors": ["only repeating chores can be skipped"]}
```

A complaint about particular fields arrives as a mapping instead, and the
message on its own says nothing:

```json
{"errors": {"instance_date": ["must be blank"]}}
{"errors": {"category_id": ["must be blank"]}}
{"errors": {"summary": ["can't be blank"]}}
```

Both were captured from a 422 on a test frame. Which shape you get depends on
the endpoint, not the status: `PUT .../chores/{id}/completions` and
`PUT .../chores/{id}` return the mapping, while a rejected `status` value on the
same completions endpoint returns the list.

`ApiError` normalizes both — `.errors` is always a list of strings, with the
field name joined onto its message, and `str(error)` is those joined with `; `.

## Verified against a live account

Every non-destructive (GET) call in pyskylight was run against a real Skylight
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

**"Up for Grabs" chores are only in `/chores/all`.** A chore with
`up_for_grabs: true` has no category — nobody owns it until somebody claims it.
`GET /chores` never returns one, in any window: querying today, today plus late,
and a full week all came back with zero uncategorized chores while
`/chores/all` held eight. `up_for_grabs` and `filter` are both rejected as query
parameters there (`422`), so there is no way to ask for them. Use
`get_all_chores()`, and `Chore.unassigned` to pick them out.

**Making a chore up for grabs takes two fields at once.** `PUT
/api/frames/{id}/chores/{id}` with `{"up_for_grabs": true}` returns `200` and
changes nothing; `{"up_for_grabs": true, "category_id": null}` works. Creating
one directly is not possible — `POST /chores` answers `422 Category is
required.` whether or not `up_for_grabs` is set, with or without an explicit
null category.

**Completing a chore: whether `category_id` belongs in the body depends on the
chore.** Verified on a test frame, both directions:

| Chore | `category_id` sent | Result |
| --- | --- | --- |
| Assigned | yes | `422` |
| Assigned | no | `200`, `completed_category` set to the chore's own category |
| Up for grabs | yes | `200`, `completed_category` set to that category |
| Up for grabs | no | `422` |

So an up-for-grabs chore cannot be completed anonymously: the API insists on
knowing who claimed it. `completed_category_id`, `completed_category`, and
`completed_by` are all rejected — `category_id` is the name.

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

pyskylight makes all of these required arguments.

**Relationship names vary by endpoint.** `calendar_events` side-loads
`categories` (plural, to-many) while `countdowns` returns `category` (singular).
`CalendarEvent` exposes both `category_id` and `category_ids`.

**Attribute coverage** is much wider than either source documented — frames carry
36 attributes (sleep schedule, slideshow settings, feature bundle, share token),
devices 24 (including nightlight and sleep sound), chores 22, calendar events 23.
All are now modeled.

## Write calls, verified against a test frame

All write endpoints were then exercised against a dedicated throwaway frame —
34/34 succeed, with every created object deleted afterwards. This overturned the
biggest assumption carried over from the OpenAPI spec.

**Writes are not JSON:API.** The spec documents `POST /chores` taking
`{"data": {"type": "chore", "attributes": {...}}}`, and ships a captured example
of exactly that. The live API ignores it — the wrapper is silently dropped and
the request fails validation on the (now missing) fields:

```
POST /lists  {"data": {"type": "list", "attributes": {"label": "x", ...}}}
422 Label can't be blank; Kind can't be blank; Color can't be blank
```

Every create and update takes a **flat** body instead: `{"label": "x", "kind":
"shopping", "color": "#00526D"}`. That holds for categories, chores, task box
items, lists, list items, calendar events, rewards, nudges, and albums. The
spec's example is presumably from an older API version.

**Singular vs plural category fields.** `POST /chores` takes `category_id` and
rejects `category_ids`; `POST /chores/create_multiple` is the reverse and takes
`category_ids` — that is what "multiple" means, one chore per profile, not a
batch of different chores. A `{"chores": [...]}` array returns a 500. Rewards,
nudges, and reward points all take `category_ids`.

**A category is mandatory** for chores, rewards, nudges, and reward points
(`422 Category is required` / `Category ids is required`).

**Completions.** `PUT /chores/{id}/completions` accepts `status` values
`"complete"` and `"pending"` — **not** `"completed"`, and not `"skipped"`, both
of which fail `status is not included in the list`. `instance_date` is required
for recurring chores and rejected for one-time ones. `category_id` is rejected
outright (`must be blank`) — except on an up-for-grabs chore, where it is what
records who claimed it.

**`instance_time` is required too, for a chore with a time of day.** Verified on
a live frame: a recurring chore with `start_time: "06:00"` answers `422
instance_time can't be blank` when only `instance_date` is sent. Pass
`start_time` back unchanged — `"06:00"` is accepted, and the value is what
distinguishes the two occurrences of a chore that repeats morning and evening.
The occurrence id shows the same thing: an untimed chore is
`"<chore_id>-<date>"`, a timed one `"<chore_id>-<date>-<HHMM>"`.

**`apply_to` is conditional.** `DELETE /chores/{id}` rejects it on a one-time
chore with `400 one-time chores should not have a value for apply_to`, and needs
it for recurring ones. It is optional on update. pyskylight defaults it to unset.

**Move takes a neighbour, not an index.** Every scalar form of `position` fails
with `422 Position is required` — including query and form encodings. The real
shape is an object: `{"position": {"before": <chore_id>}}` or `{"after": ...}`.
The unhelpful error comes from the object-shape check, which reports
`position must include at least one of \`before\` or \`after\`` only once
`position` is a dict.

**Field names.** Albums take `title`, not `name`. Nudges take `body` and
`deliver_at`, not `summary` and `start` — and they turn out to be spoken
reminders, with `voice_kind` and `audio_url` fields.

**Create responses vary.** `POST /chores` and most creates return a single
resource under `data`; `POST /chores/create_multiple` and `POST /rewards` return
a **list**. pyskylight returns `list[Chore]` and `list[Reward]` for those two.

**Colors are validated** against the palette from `GET /api/colors`; an
arbitrary hex is rejected with `Color is invalid`.

## Meals, verified against a test frame

**A recipe's name is `summary`.** There is no `title`. `description` is one free
text field carrying both halves, in the loose shape the app writes:

```
Ingredients:
- Cereal
- Milk

Instructions:
1. Pour milk over cereal and enjoy.
```

Nothing enforces that shape, and there is no structured ingredient list anywhere
on the resource.

**`meal_category_id` is required to create one**, and its absence is a bare
`422 Unprocessable Entity` naming no field — unlike most validation failures
here, which say what is missing. The four categories (Breakfast, Lunch, Dinner,
Snack) come with the frame; nothing observed creates a fifth.

**`add_to_grocery_list` is asynchronous.** It returns the recipe immediately,
with `meta.auto_creation_intent_id` and a matching `tool_call_id` — Skylight
parses the ingredients out of the free text server-side, and the items appear a
few seconds later. Re-reading the list straight after the call shows nothing;
about ten seconds in, `- Tortillas / - Ground beef / - Salsa` had become three
list items.

**The destination is not a choice.** Ingredients always land on the list whose
`default_grocery_list` is set. Verified by giving a frame a second shopping list
and adding a recipe: the default took all three items and the second stayed
empty. A client offering to pick a list would be lying.

## The photo feed, verified against a live account

Skylight calls photos **messages**. `GET /frames/{id}/messages` returns them
newest first, 30 to a page, with `meta.current_page` and `meta.num_pages` — 180
photos across 6 pages on the account tested.

**The page size is not negotiable.** `page` selects a page; `per_page` and
`limit` are both accepted and ignored, and a JSON:API-style `page[size]` is a
404. So a client wanting the newest photo asks for page 1 and takes the first
entry.

**Asset URLs are signed and expire.** `asset_url` and `thumbnail_url` are
CloudFront URLs carrying an `Expires` roughly a week out, minted fresh on each
read — fetch them, do not store them.

Every message observed had `asset_type: "photo"`, and captions were empty
throughout, so the field is real but unexercised on that account. Frames do
support video, so other asset types presumably exist and are not characterised
here.

## Nudges, verified against a test frame

A nudge is a spoken reminder: the frame reads the `body` aloud at `deliver_at`,
to the profiles in `category_ids`.

**A calendar display never plays one.** Two nudges were sent to a real
`15-CAL-2.0` targeting a family member — one with `deliver_at` set to now, one
scheduled two minutes ahead. Neither was heard, and neither appeared on the
frame, while every API-side signal said it had worked: the resource was created,
the audio rendered, the listing showed it.

This is most likely the Buddy split that alarms make explicit with `422 Device
must be a buddy device`. Alarms hang off a *device*, so there is something to
validate against; nudges hang off the *frame*, where nothing knows what hardware
will have to speak them, so the write is accepted regardless. Treat a successful
`create_nudge` as no evidence that anybody will hear it.

**The speech is rendered in the cloud.** `audio_url` is `null` on the created
resource and holds a presigned S3 URL for `nudge_<id>.mp3` within about ten
seconds. The URL is signed per read with a short expiry, so it is a thing to
fetch, never a thing to store.

**Both `deliver_at` and `category_ids` are required**, and neither is validated
beyond being present: an empty list is `422 Category ids is required`, while a
`deliver_at` in the past is accepted without complaint. Whether the frame plays
a nudge whose time has already passed is unknown — it cannot be observed through
the API, and needs a real frame within earshot.

**`voice_kind` defaults to `kirk_voice`.** An unknown value returns a
`500 Internal Server Error` rather than a validation error, so the valid set
cannot be enumerated by probing, and there is no endpoint listing voices the way
`GET /api/colors` lists the palette.

**Delivered nudges are not cleaned up.** They stay listed indefinitely, so the
listing is a history as well as a schedule.

**`after` and `before` are both required** (`422 After/Before is required`), and
`before` behaves as an instant at midnight UTC rather than as an inclusive day:
a nudge at `2026-08-09T03:01Z` is absent from a query with `before=2026-08-09`,
even though that instant is the evening of the 8th in the frame's own timezone.
To cover a day, pass the day after it. Only that one boundary was tested.

## Device settings, verified against live hardware

Tested against a real, activated display, each write read back and restored.

**Display settings live on the device, not the frame.** `PUT /api/frames/{id}`
accepts `brightness`, `sleeps_at`, `slideshow_speed`, `show_caption` and the
rest, returns `200` — and applies **none** of them. The same fields sent to
`PUT /api/frames/{frameId}/devices/{deviceId}` work. A silent no-op is the worst
kind of failure for a client, so `update_frame()` carries a warning and
`update_device()` is the method to reach for.

**Writable on a device** (all verified, changed and restored): `name`,
`brightness`, `nightlight`, `nightlight_brightness`, `sleep_sound_volume`,
`sleeps_at`, `wakes_at`, `slideshow_speed`, `show_caption`, `blur_effect`,
`side_by_side`, `show_heart`.

**`nightlight_color` is an enum.** Accepted: `off`, `red`, `orange`, `yellow`,
`green`, `blue`, `pink`. Rejected with `422 Nightlight color is not included in
the list`: `white`, `warm`, `purple`. Modeled as `NightlightColor`.

**`sleep_mode` accepts only its current value.** Every other candidate —
`off`, `nightlight`, `dim`, `clock_only`, `photo`, `sleep_sound` — returns
**HTTP 500**, not a 422. A server error rather than a validation error hints the
other modes need something else configured first, so treat this field as
read-only until that is understood.

**Renaming a frame is blocked for activated hardware:**
`422 Contact help@myskylight.com to rename this device`. It succeeds on a frame
with no display attached, which is why the earlier write testing missed it.

**Alarms need a Buddy device.** `POST
/api/frames/{id}/devices/{id}/alarms` on a calendar display returns
`422 Device must be a buddy device`, whatever the body — an empty object, a
`time`, or a `name` all fail identically, before any field validation runs. So
the alarm body remains uncaptured, and `Alarm` still exposes only
`attributes`.

Skylight Buddy is a separate product; a `15-CAL-2.0` calendar is not one.
`GET` and `DELETE` on the alarms collection work on a non-Buddy device and
simply report none.

**`role == "buddy"` is the discriminator.** The device attributes carry no
`buddy` flag, but they do carry `role`, `null` on a calendar. That was a guess
here until the vendor's own web client confirmed it — its `deviceUtils.isBuddy`
is exactly:

```js
function t(t){return !!t && 'buddy' === t.attributes.role}
```

and `areAllBuddyDevices` (every device passing that test) is what routes the app
to its Buddy screens instead of its calendar ones.

**The nightlight and sleep sound are Buddy features, and the API will not tell
you so.** This is the trap worth reading before trusting a `200` from
`update_device`. On a `15-CAL-2.0`, in a single run:

| Call | Result |
| --- | --- |
| `POST .../devices/{id}/alarms` | `422 Device must be a buddy device` |
| `GET .../devices/{id}` | returns `nightlight`, `nightlight_brightness`, `nightlight_color`, with stored non-default values |
| `PUT nightlight: true` | `200`, re-read confirms |
| `PUT nightlight_brightness: 33` | `200`, re-read confirms |
| `PUT nightlight_color: "green"` | `200`, re-read confirms |
| `PUT nightlight_color: "purple"` | `422 Nightlight color is not included in the list` |

Present, writable, persisted across an independent `GET`, and enum-validated —
on the same device, minutes apart from the alarm rejection. Every server-side
signal says these fields work. They are still Buddy-only:

- The app renders the nightlight toggle and its brightness slider in exactly one
  place, its Buddy sleep screen, under the label key `buddy:label.nightlight`,
  alongside Buddy sleep sounds and built from `buddyConstants`.
- `nightlight_color` appears **nowhere** in the app bundle. No Skylight client
  reads or writes it on any device.
- Sleep sounds are the same story: `buddyConstants.sleepSounds` is
  `brown_noise`, `ocean_waves`, `rain`, `stream`, `white_noise`, offered only on
  that Buddy screen. A calendar reports `sleep_sound: null` with a stored
  `sleep_sound_volume`.

So the Buddy check that rejects alarms does not extend to these fields — they
are simply columns the server will store for anything. A `200` means the value
was persisted, not that any hardware acts on it, and a client that treats
"writable" as "supported" will offer controls that do nothing. Gate on `role`.

**`sleep_mode` is `screen_off` or `dim_clock`**, from the same `buddyConstants`.
The 500s recorded above came from guessing at names — `off`, `nightlight`,
`dim`, `clock_only`, `photo`, `sleep_sound` are all invalid, and `dim_clock` was
never among the values tried. Untested against live hardware.

**The alarm body is known after all**, from the app's `defaultAlarmAttributes`:
`time` (`"08:30"`), `hour`, `minute`, `enabled`, `volume`, `sound`
(`"marimba"`), `label`, `snoozable`, `rrule`, `fires_on`. Still unverified — the
endpoint refuses a non-Buddy device before it looks at the body — so `Alarm`
continues to expose only `attributes`.

**There is no capability map covering any of this.** Frames carry a
`feature_bundle` — `albums`, `chores`, `timers`, `screensaver` and about twenty
more, each `{"enabled": bool}`, under a `bundle_name` (`cal_plus` on a Plus
calendar). Neither alarms nor nightlight appear in it. `role` is the only
signal.

**`hardware_model` is on the frame, not the device, and only in the detail
endpoint.** `GET /api/frames/{id}` returns `"15-CAL-2.0"`; `GET /api/frames`
omits the field entirely, and no device attribute carries a model at all. It is
also not what the app gates on, so prefer `role` for capability questions.

**Occasional 500s.** A poll against a healthy account returned `500 Internal Server
Error` once, and thirty consecutive calls across every endpoint afterwards were clean, so
it is transient rather than an endpoint being broken. Worth expecting rather than
diagnosing: callers should tolerate one, not treat it as the account being down.

## Known gaps

**Alarms** cannot be exercised without Buddy hardware, per the note above. The
schema is unknown, not merely unwritten: the server rejects the request before
it validates the body.

`reset_device` (factory reset) and `delete_device` (unpairing) are deliberately
not exercised, and `reset_device` is not implemented at all. Account-level
writes (`update_user`, `delete_user`, notification toggles) are untested too,
having no clean undo.
