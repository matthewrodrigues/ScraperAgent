# Key broker on Supabase Postgres

**Date:** 2026-09-08
**Status:** Approved for planning
**Scope:** Move the key broker's `friends` and `spend` tables from local SQLite
to Supabase Postgres, so the broker can be deployed. Supabase Auth (step 2) and
the app's own data (step 3) are out of scope.

---

## 1. Problem and goals

The key broker holds the owner's Anthropic and Apify keys and meters what each
invited friend spends. It currently persists to a local SQLite file
(`BROKER_DB_PATH`), which ties it to whichever machine holds that file.

That is the last thing keeping the broker un-deployable. Unlike the ScraperAgent
app — which drives a visible Chrome window and waits up to five minutes for a
human to click eBay's "Send Offer" — the broker is a plain HTTP service with no
browser and no human in the loop. Nothing about it needs to run at home except
its database.

Moving that database to Supabase also closes a failure this project has already
suffered: `scraperagent.db` was gitignored, unbacked-up, and got emptied with no
way to recover it. Supabase takes automated backups as a property of the
platform rather than as a script someone remembered to schedule.

**Goals**

1. The broker persists to Supabase Postgres when configured to, and can
   therefore run anywhere.
2. Tests and offline development keep working with no network and no database
   server.
3. Rolling back to SQLite requires no code change.
4. `keybroker/{auth,quota,meter,app}.py` are untouched — the storage swap is
   invisible above `db.py`.

**Success criteria**

- With `SUPABASE_DB_URL` set, `python -m keybroker` creates both tables in
  Supabase and serves proxied requests against them. Connectivity and
  credentials were verified ahead of implementation: PostgreSQL 17.6, session
  pooler on 5432, empty `public` schema.
- With it unset, behaviour is identical to today.
- The existing broker test suite passes unchanged, offline.
- A live smoke script confirms friend creation, provisional spend, settlement,
  and month boundaries against the real Supabase project.

## 2. Non-goals

Supabase Auth (step 2). Moving the app's `searches` / `listings` /
`negotiations` / `messages` tables (step 3). Row Level Security — the broker is
a trusted server-side service using its own credentials, with no per-user access
control to enforce. Dual-writing to both backends (see §5). Any change to the
proxy, clamps, quota arithmetic, or metering rules.

## 3. Architecture

**New `keybroker/dialect.py`.** `keybroker/db.py` keeps all 17 of its functions
and its SQL; it asks the dialect for a connection and a placeholder style rather
than importing `sqlite3` directly.

**Selection and lifecycle are separate concerns.** Which dialect is in use is
decided once, at import, by whether `SUPABASE_DB_URL` is set — it never changes
for the life of the process. The Postgres *connection pool*, by contrast, is
created and closed by the FastAPI lifespan (§4), not at import: importing a
module must never open sockets, or the test suite and the operator scripts would
dial Supabase merely by importing `keybroker.db`. The SQLite dialect opens
per-call connections exactly as today and needs no lifecycle at all.

**Much of the SQL stays shared**, because the installed SQLite is 3.49.1:

| Construct | Both backends? |
|---|---|
| `ON CONFLICT … DO UPDATE` (the `record_spend` upsert) | Yes — SQLite >= 3.24 |
| Partial unique index `WHERE upstream_ref IS NOT NULL` | Yes — SQLite >= 3.8 |
| `RETURNING id` (replaces `cur.lastrowid`) | Yes — SQLite >= 3.35 |

So the upsert, its conflict target, and `create_friend` are written once.

**What the dialect absorbs:**

| Difference | SQLite | Postgres |
|---|---|---|
| Connect + row shape | `sqlite3.connect`, `sqlite3.Row` | `psycopg` pool, `dict_row` |
| Placeholder | `?` | `%s` |
| Primary key | `INTEGER PRIMARY KEY AUTOINCREMENT` | `GENERATED ALWAYS AS IDENTITY` |
| Money column | `REAL` | `DOUBLE PRECISION` |
| Timestamp column | `TEXT DEFAULT (datetime('now'))` | `TIMESTAMPTZ DEFAULT now()` |
| Column introspection | `PRAGMA table_info` | `information_schema.columns` |
| Multi-statement DDL | `executescript` | loop over statements |

`provisional` remains `INTEGER` on both rather than becoming a Postgres
`BOOLEAN`, so calling code never has to know which backend it is on.

**Dependency:** `psycopg[binary]` and `psycopg_pool`. Nothing else.

## 4. Configuration and connections

**`SUPABASE_DB_URL`** (empty by default) selects the backend: set means
Postgres, unset means SQLite at `BROKER_DB_PATH`. There is no separate mode
flag, so the two cannot disagree. A value that is set but unparseable fails at
startup rather than silently falling back to a local file nobody is reading —
the same fail-closed stance `api/auth.py` takes on an unset
`DASHBOARD_PASSWORD`.

It contains a password, so it lives in `.env` and the deployment platform's
secret store. `.env.example` gets the key with an empty value and a comment
pointing at Supabase's connection-string page.

**Connection pooling.** An in-process `psycopg_pool`, opened and closed by the
FastAPI lifespan that already owns the httpx client. The broker issues roughly
five queries per proxied request — token lookup, two quota sums, the clamp's
remaining-budget read, and the meter write. Unpooled, that is five TCP and TLS
handshakes per Claude call.

**Use Supabase's session pooler (port 5432), not the transaction pooler
(6543).** The broker is one long-lived process, so session mode — one backend
per connection for that connection's life — is what an in-process pool wants.

An earlier draft recommended the *direct* connection. **That is not usable in
this deployment.** Verified against the real project: `db.<ref>.supabase.co`
publishes an `AAAA` record only, and the owner's machine has no working global
IPv6 route, so the hostname resolves to nothing that can be reached. Supabase
made direct connections IPv6-only without the paid IPv4 add-on. The session
pooler (`aws-0-<region>.pooler.supabase.com`) publishes `A` records and is
reachable. Note its username differs: `postgres.<project-ref>`, not `postgres`.

The pool sets `prepare_threshold=None` regardless, because psycopg 3
auto-prepares statements after a few executions and pgBouncer's *transaction*
mode rejects that — so a later move to port 6543 is a URL change rather than a
debugging session.

**The design is indifferent to which of the three modes is used**, because
§4.1 removed all dependence on session state. That was decided for correctness
under pooling; it also meant this connectivity blocker cost a string swap rather
than a redesign.

### 4.1 Timestamps must not depend on session state

`month_bounds` builds naive strings like `"2026-09-01 00:00:00"` and compares
them against `created_at`. SQLite's `datetime('now')` is always UTC, so this is
correct today.

Postgres will happily cast that literal — the query *works* — but `now()`
returns time in the **session's** timezone. A non-UTC session shifts every month
boundary by hours and files spend in the wrong month. Nothing errors.

An earlier draft fixed this with `SET TIME ZONE 'UTC'` on connect. **That is
rejected**: it is session state, and under a transaction pooler each transaction
may get a different backend connection, so a `SET` issued once does not reliably
apply later. The bug would appear only under load, only near month boundaries,
and only in production.

**The fix lives in the query instead.** Postgres comparisons read
`created_at >= (%s AT TIME ZONE 'UTC')`, pinning the interpretation explicitly
and correctly whether or not any session setup ran. `month_bounds` keeps
returning naive strings and is unchanged. This holds on the direct connection
and on the pooler.

## 5. Migration and rollback

**There is effectively nothing to migrate.** The live `broker.db` holds one
friend — a revoked test account — and zero spend rows. No real friend has been
issued a live token.

**So no migration tool is built.** Point `SUPABASE_DB_URL` at an empty project,
let `init_db()` create the schema, and issue tokens there.

**The condition that changes that answer:** if tokens are issued to real friends
before cutover, migration stops being optional — not because the rows are
precious, but because tokens are unrecoverable by design. Only `token_sha256` is
stored, so a token cannot be re-printed; skipping migration would mean revoking,
reissuing, and messaging every friend. In that case the fallback is a one-shot
script copying `friends` and the current month's `spend`; hashes migrate cleanly
and existing tokens keep working. That script is written when it is needed, not
speculatively.

**Cutover sequence**

1. Create the Supabase project; take the direct connection string (5432).
2. Set `SUPABASE_DB_URL` locally and run `python -m keybroker`; `init_db()`
   builds both tables.
3. `python -m scripts.add_friend <name>`; confirm the row lands in Supabase.
4. Run the broker suite (SQLite, unchanged) plus the live smoke script (§7).
5. Deploy with the URL in the platform's secret store.

**Rollback is free: unset `SUPABASE_DB_URL`.** No code change, no different
build, no schema surgery — a direct consequence of keeping the SQLite path.

**What rollback loses:** spend recorded in Postgres does not flow back to
SQLite, so rolling back mid-month resets apparent spend to whatever the local
file last knew, reopening caps. Acceptable as an emergency lever; documented as
one-way in practice rather than a routine toggle.

**Dual-writing to both backends is rejected.** It would keep SQLite warm as a
rollback target at the cost of doubling the failure surface: every write could
half-succeed, and two ledgers could disagree about a friend's remaining budget
with no arbiter. A cap enforced by two sources of truth is worse than one.

## 6. Error handling

The existing failure asymmetry is preserved and made explicit, because it
matters more once the database is across a network.

| Failure | Behaviour | Rationale |
|---|---|---|
| Postgres unreachable at startup | 503 on every proxied route | Same stance as an unset vendor key |
| DB unreachable during auth or quota | **503 — do not forward** | If the cap cannot be checked, spending would be uncapped |
| Pool exhausted / checkout timeout | 503 | As above |
| Metering write fails after a successful upstream call | **Forward the response, log ERROR** | The money is already spent; withholding the result costs the friend both |

The last row inverts the others deliberately. Auth and quota gate spending and
so fail closed; metering is bookkeeping after spending and so fails open. This
is today's behaviour, now load-bearing in a world where the database can be down
while Anthropic is reachable.

Startup failure is distinguishable from a transient one in the logs: an
unparseable or unreachable `SUPABASE_DB_URL` logs once at ERROR naming the
setting, so a misconfigured deploy is diagnosable from the platform's log tail
without shell access.

## 7. Testing

Tests keep running on SQLite, which means the backend that runs in production is
not the one CI exercises. That limit is named rather than papered over, and
addressed in three layers of decreasing coverage.

1. **The existing broker suite runs unchanged on SQLite.** No test edits. This
   is itself the regression check that the refactor changed no behaviour.
2. **Dialect unit tests, no database required.** Assert the shim's outputs
   directly: placeholder style, the DDL each dialect emits, the introspection
   query, and that `AT TIME ZONE 'UTC'` appears in the Postgres month
   comparison.
3. **Contract tests parameterized over both backends.** One set of bodies
   covering `create_friend`, the `record_spend` upsert, provisional settling,
   and month boundaries — run against SQLite always, and against Postgres only
   when a database URL is configured for tests, skipped otherwise. CI stays
   offline; the full matrix runs locally before a deploy with one env var.

   **That variable is `SUPABASE_TEST_DB_URL`, deliberately NOT
   `SUPABASE_DB_URL`.** These tests create friends, write spend rows, and
   truncate between cases. Reusing the production setting would mean that
   configuring the broker for real use silently arms the test suite to write
   junk rows into — and delete rows from — the live ledger. A separate name
   makes pointing tests at production a deliberate act rather than a default.
   The tests refuse to run if the two URLs are equal.

**Three things are structurally untestable on SQLite**, and they are exactly
where this migration can go wrong: the DDL (identity columns, `TIMESTAMPTZ`,
`DOUBLE PRECISION`), the `AT TIME ZONE 'UTC'` comparison, and
`information_schema`-based column migrations. Layer 3 exists for those, and is
parameterized rather than a separate Postgres-only file that would drift.

**The month-boundary test is the one to run against real Postgres before
trusting a deploy.** It is the only failure mode here that is silent, wrong by
hours, and visible only near the 1st of a month.

**Live smoke script `scripts/smoke_supabase.py`**, mirroring
`scratch_ebay_search.py`'s role: connect to the real project, create and revoke
a throwaway friend, write and settle a provisional spend row, and confirm month
bounds land in the right month. This is the check that validates against reality
rather than against assumptions about Postgres.

## 8. Accepted risks and follow-ups

- **CI never exercises Postgres.** Mitigated by layers 2 and 3 above plus the
  smoke script, but a Postgres-only defect can still reach a deploy. Running the
  parameterized suite against a Supabase test project in CI is the obvious
  hardening, deferred because it requires a second project and a CI secret.
- **Rollback loses the interim ledger** (§5).
- **Latency rises.** Local SQLite reads are microseconds; Supabase is a network
  round trip. Pooling removes the handshake, not the round trip. Acceptable for
  a broker doing about five queries per request; it is the app's ~34 per-call
  connection sites that would need attention if step 3 ever proceeds.
- **Follow-up — step 2:** Supabase Auth for the portal, which may retire the
  broker's own `sa_` token scheme in favour of Supabase JWTs.
- **Follow-up — step 3:** moving the app's data is a materially different
  decision, since it changes a local-first tool into one that cannot operate
  offline and puts friends' negotiation transcripts in the owner's custody.
