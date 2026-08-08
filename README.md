# pyskylight

An async Python client for the [Skylight](https://www.skylight.com/) API — calendars,
chores, lists, rewards, and frames.

> **Unofficial.** Not affiliated with or endorsed by Skylight. The API is
> reverse-engineered from observed traffic and may change without notice. Use it
> only with accounts you own.

## Install

```bash
uv add pyskylight
```

```bash
pip install pyskylight
```

## Quick start

```python
import asyncio

from pyskylight import PasswordAuth, Skylight


async def main() -> None:
    async with Skylight(PasswordAuth("me@example.com", "hunter2")) as skylight:
        frame = (await skylight.get_frames())[0]

        for chore in await skylight.get_chores(frame.id, after="2025-08-25", before="2025-08-31"):
            print(chore.summary, chore.start, "done" if chore.completed else "todo")

        for family_list in await skylight.get_lists(frame.id):
            print(family_list.label, family_list.kind)


asyncio.run(main())
```

`Skylight` creates and owns an `aiohttp.ClientSession` unless you pass one in:

```python
async with aiohttp.ClientSession() as session:
    skylight = Skylight(PasswordAuth(email, password, session=session), session=session)
```

## Authentication

Skylight uses OAuth 2.0 authorization code + PKCE, with credentials entered into a
server-rendered Rails login form. `PasswordAuth` drives that whole flow headlessly and
refreshes the access token before it expires:

1. `GET /oauth/authorize` → redirect to `/auth/session/new`, which carries a Rails CSRF
   token in `<meta name="csrf-token">` and sets a `skylightcloud_session` cookie.
2. `POST /auth/session` with `authenticity_token`, `email`, `password`.
3. `GET /oauth/authorize` again (now authenticated) → redirect to
   `https://ourskylight.com/welcome?code=...&state=...`.
4. `POST /oauth/token` exchanges the code plus the PKCE `code_verifier` for an
   access token and a refresh token.

pyskylight never follows the final redirect — it reads the authorization code out of the
`Location` header — and the Rails session cookie is confined to a private cookie jar.

If you already captured a token, skip the flow:

```python
from pyskylight import Skylight, TokenAuth

skylight = Skylight(TokenAuth("<access token>"))
```

`TokenAuth` cannot refresh; a rejected token raises `AuthenticationError`.

Sign out with `await auth.revoke()`.

## Common operations

```python
# Family profiles ("categories" in the API)
categories = await skylight.get_categories(frame_id)

# Chores
chore = await skylight.create_chore(
    frame_id,
    "Take out recycling",
    categories[0].id,  # a chore must belong to a profile
    start="2025-09-01",
    start_time="10:00",
    recurring=True,
    recurrence_set="RRULE:FREQ=WEEKLY;INTERVAL=2;BYDAY=MO;WKST=SU",
)
await skylight.complete_chore(frame_id, chore.chore_id, instance_date="2025-09-01")
await skylight.delete_chore(frame_id, chore.chore_id, apply_to=ApplyTo.ALL)  # recurring only

# Lists
grocery = await skylight.get_list(frame_id, list_id)  # items + sections resolved
await skylight.create_list_item(frame_id, grocery.id, "Milk")

# Calendar
events = await skylight.get_calendar_events(
    frame_id, date_min="2025-09-01", date_max="2025-09-30", timezone="America/Los_Angeles"
)
```

Recurring chores are returned one resource per occurrence. `Chore.id` is the occurrence
id (`"<chore_id>-<date>"`); pass `Chore.chore_id` — the `group` attribute — when updating,
deleting, or completing.

A few endpoints don't follow the usual shapes, and pyskylight normalizes them:

```python
groups = await skylight.get_all_chores(frame_id)  # ChoreGroups, bucketed
groups.chores["late"], groups.chores["today"], groups.routines["today_timed"]
groups.all  # flattened

balances = await skylight.get_reward_points(frame_id)  # plain array upstream
frames = await skylight.get_calendar_frames()  # a list, despite the path
```

Some endpoints reject requests that omit an optional-looking parameter, so pyskylight makes
those required: `get_countdowns(frame_id, timezone)`, `get_nudges(frame_id, after, before)`,
`get_meal_sittings(frame_id, date_min, date_max)`.

Display settings belong to the device, not the frame: `update_frame()` accepts them and
silently applies nothing, while `update_device()` works. Write calls send flat bodies, not
JSON:API documents, and several have sharp edges the
published spec does not mention — `"complete"` rather than `"completed"`, `apply_to` being
forbidden on one-time chores, `move_chore` taking a neighbour instead of an index. All of
it is verified against a live test frame and written up in
[docs/api-notes.md](docs/api-notes.md).

## Models and unmodeled fields

The upstream schema is observed, not specified, so every model keeps its raw resource:

```python
chore.attributes["a_field_pyskylight_does_not_know_about"]
```

Fully typed models, all verified against live responses: `Frame`, `Category`, `Chore`,
`TaskBoxItem`, `SkylightList`, `ListItem`, `Device`, `CalendarEvent`, `SourceCalendar`,
`Reward`, `RewardPoint`, `Nudge`, `User`. Thin models (id plus `.attributes`) where the
account used for verification had no data to capture: `Alarm`. Endpoints whose shape is
entirely unknown (meals, photos, Plus, activities) return the decoded JSON untouched.

Anything not wrapped is still reachable:

```python
data = await skylight.request("GET", f"/api/frames/{frame_id}/month_in_review")
```

## Errors

| Exception | When |
|---|---|
| `AuthenticationError` | Login failed, or the token was rejected and could not be refreshed |
| `NotAuthorizedError` | HTTP 401/403 after one refresh attempt |
| `NotFoundError` | HTTP 404 |
| `RateLimitError` | HTTP 429 |
| `ApiError` | Any other unsuccessful status |

All derive from `SkylightError`. `304 Not Modified` and `204 No Content` return `None`
(empty lists for list endpoints).

## Development

This project uses [uv](https://docs.astral.sh/uv/). One command sets up a virtualenv
with the locked dependency versions:

```bash
uv sync
```

```bash
uv run pytest
```

```bash
uv run ruff check . && uv run ruff format --check . && uv run mypy pyskylight
```

Dev tools live in the `dev` [dependency group](https://peps.python.org/pep-0735/), which
`uv sync` installs by default and which stays out of the published wheel. `uv.lock` is
committed and CI runs `--frozen`, so a new upstream release can't turn a green branch red
on its own; run `uv lock --upgrade` to pick up newer versions deliberately.

Enable the git hooks (ruff, mypy, pytest, lockfile freshness, and a guard against
committing credentials) once:

```bash
uv run pre-commit install
```

Test against another interpreter with `uv run --python 3.10 pytest`, and build with
`uv build`.

CI runs the suite on 3.10–3.13 with branch coverage (floor: 97%), the linters, the
pre-commit hooks, a build with `twine check`, and a job that installs the declared
dependency floor (`aiohttp==3.9.0`) to check that claim is true.

## Releasing

Publishing runs from CI via [PyPI Trusted Publishing](https://docs.pypi.org/trusted-publishers/),
so no API token is stored in the repository. One-time setup on PyPI (Account → Publishing):
owner `dknowles2`, repository `pyskylight`, workflow `release.yml`, environment `pypi`.

To cut a release, publish a GitHub release tagged `vX.Y.Z`. That is the whole process —
there is no version to bump, because `hatch-vcs` takes it from the tag.

A build from an untagged commit is versioned from the last tag with a `.devN` suffix and a
local `+g<sha>` segment, which PyPI refuses outright — so a stray publish cannot masquerade
as a real release. The workflow still checks the tag against the built version, which now
catches a shallow clone rather than a forgotten edit.
`workflow_dispatch` publishes to TestPyPI for a dry run.

## Sources

The endpoint surface comes from two reverse-engineering efforts; see
[docs/api-notes.md](docs/api-notes.md) for how they differ and which one pyskylight
follows where they disagree.

## License

Apache-2.0
