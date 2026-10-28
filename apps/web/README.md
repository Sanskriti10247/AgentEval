# AgentEval dashboard

Next.js (App Router) + TypeScript + Tailwind CSS 4 + TanStack Query + Recharts. It talks only to the public REST API (`/api/v1`), directly from the browser. There is no server-side data access and no analytics. The only secret it handles is the user's API key, kept in `sessionStorage` (see below).

## Run it

```bash
# from the repository root
make up && make migrate     # API on :8000; `make web-up` adds the dashboard container on :3000
make web                    # dev server on :3000 (npm ci && npm run dev)
make web-check              # typecheck + lint + tests + production build
```

Or by hand: `cd apps/web && npm ci && npm run dev`. If the API is down, every page shows a readable error with a Retry button instead of crashing.

| Variable | Default | Notes |
|---|---|---|
| `NEXT_PUBLIC_API_URL` | `http://localhost:8000` | Origin of the API. Inlined at build time (the Docker build arg is `NEXT_PUBLIC_API_URL`, set from `AGENTEVAL_WEB_API_URL` in compose). The API must allow the dashboard's origin in `AGENTEVAL_CORS_ORIGINS` (default `http://localhost:3000`). |

## API key

The API requires a key (ADR-012). On first use the dashboard shows an "API key" screen (`components/auth-gate.tsx`). The key is stored in `sessionStorage` under `agenteval.apiKey` (cleared when the tab closes, never in a cookie or `localStorage`), sent as `Authorization: Bearer <key>` by `lib/api/client.ts`, and forgotten by **Sign out** in the sidebar. A 401 clears the stored key and returns to the key screen with a message; a 403 (cancel and replay need the `write` scope) shows "Not permitted" with the API's explanation and keeps the key; a 429 shows how long to wait. Create a key with `docker compose exec api python -m agenteval_api.keys create --name dashboard --scopes read,write`. With `AGENTEVAL_AUTH_ENABLED=false` on the API, enter any value.

Before asking for a key, the dashboard reads `GET /health`. If the API answers `"read_only": true` (`AGENTEVAL_READ_ONLY=true`), it shows the pages without a key, hides Cancel, Replay, the launch forms and Sign out, and says "Read-only demo" in the sidebar. If it answers `"public_demo": true`, it also skips the key screen, but offers New evaluation and Start a run on the agents, datasets and tasks already there, plus cancel and replay; `demo_limits.max_repetitions` caps the repetitions field, and `concurrent_runs` explains why a large batch waits in the queue. The API never reports both flags; if it did, `read_only` would win. If `/health` fails or says nothing, the key screen works as described above. Any script running on the dashboard's origin can read `sessionStorage`, so serve it over HTTPS and from an origin you control.

## Pages and the API they use

| Page | Route | Endpoints |
|---|---|---|
| Dashboard | `/` | `GET /stats/overview?window_h=` (24 h / 7 d / 30 d), refreshed every 10 s |
| Agents | `/agents`, `/agents/[id]` | `GET /agents`, `/agents/{id}`, `/agents/{id}/versions`, `/evaluations?agent_version_id=`, `/evaluations/{id}/summary` |
| Tasks | `/tasks` | `GET /tasks?dataset_id=&task_type=&difficulty=`, `/datasets` |
| Evaluations | `/evaluations`, `/evaluations/[id]` | `GET /evaluations`, `/evaluations/{id}`, `/summary`, `/results`; `POST /evaluations/{id}/cancel` |
| Compare | `/evaluations/compare?ids=a,b,c` | `GET /evaluations/compare?ids=` (2 to 5, same dataset and scoring hash) |
| New evaluation | `/evaluations/new` (also `?agent_version_id=`) | `GET /agents`, `/agents/{id}/versions`, `/datasets?status=FROZEN`; `POST /evaluations` with an `Idempotency-Key` |
| Runs | `/runs` (also `?status=&dead_letter=true&evaluation_id=&task_id=`) | `GET /runs`, `/tasks` (names) |
| Start a run | `/runs/new` (also `?agent_version_id=&task_id=`) | `GET /tasks`, agents and versions; `POST /runs` with an `Idempotency-Key` |
| Run detail | `/runs/[id]` | `GET /runs/{id}`, `/tasks/{id}` (title), `/attempts`, `/attempts/{n}/events` (paged by `after_seq`), `/attempts/{n}/evaluation`; `POST /runs/{id}/cancel`, `/replay` |
| Workers | `/workers` | `GET /workers`, refreshed every 5 s |

Lists use keyset pagination: each page is `{<resource>: [...], next_after}` and "Load more" passes `next_after` back as `after`. Trajectory events use `next_after_seq` / `after_seq`.

Notes on what the read API does not provide, and how the UI copes:
- An evaluation carries `agent_version_id` only. The UI resolves it to an agent name and version by listing agents and their versions (`useVersionIndex`), and falls back to the short id.
- Evaluation list rows get success rate, latency and cost from one `GET /evaluations/{id}/summary` per row.
- Run and result rows carry task ids only; the evaluation detail page maps them to names through the summary's `per_task`.
- Decimals (`cost_usd`, scores) arrive as strings; the client keeps them as strings and formats at render time.

## Layout

```
app/                 routes (thin server components that pass params to views), layout, globals.css (theme tokens)
components/views/    one client component per page (data fetching with TanStack Query)
components/ui.tsx    status marks, sections, readouts, meters, tables, controls, loading / error / empty states
components/launch/   New evaluation and Start a run forms
components/brand.tsx the mark and wordmark (favicon: app/icon.svg)
lib/api/types.ts     hand-written types mirroring the backend's public schemas
lib/api/client.ts    typed fetch client (ApiError, problem+json parsing, bearer header, one function per endpoint)
lib/auth.ts          the API key in sessionStorage (+ the rejected flag after a 401)
lib/hooks.ts         query and mutation hooks (live refresh while runs are in progress)
lib/format.ts        durations, cost, percentages, timestamps
tests/               vitest + Testing Library with a mocked fetch
```

Design: graphite ink on cool paper in light mode, a true graphite in dark mode; the OS preference decides unless the reader picks Light or Dark in the sidebar (stored in `localStorage`, applied before first paint). IBM Plex Sans for text, Plex Mono for ids, numbers, durations and trajectories, Plex Sans Condensed for page titles, all self-hosted by `next/font`. Hairline rules instead of boxes, 2 to 3 px radii, one accent (signal orange) for the active nav item, focus rings, live markers and the overall score. Status is a small glyph whose shape and colour both encode the tone (`components/ui.tsx` `Glyph`, colours in `globals.css`, mapping in `lib/status.ts`). The only motion is the pulsing "live" dot, which respects `prefers-reduced-motion`.

Launch forms (`components/launch/`) validate inline before posting, send one `Idempotency-Key` per distinct submission (a double click queues one batch), and show 403, 409, 422 and 429 answers as a plain sentence next to the button. On success they navigate to the new evaluation or run, which polls while it is active.

## Checks

```bash
npm run typecheck && npm run lint && npm test && npm run build
```

Dependencies are pinned to exact versions; `npm audit` reports advisories in the dev-only lint toolchain (`eslint-config-next` -> `fast-glob` -> `braces`); none ship in the runtime bundle.
