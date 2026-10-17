# AgentEval dashboard

Next.js (App Router) + TypeScript + Tailwind CSS 4 + TanStack Query + Recharts. It talks only to the public REST API (`/api/v1`), directly from the browser. There is no server-side data access, no secrets and no analytics. **There is no authentication yet (Phase 8).**

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

## Pages and the API they use

| Page | Route | Endpoints |
|---|---|---|
| Dashboard | `/` | `GET /stats/overview?window_h=` (24 h / 7 d / 30 d), refreshed every 10 s |
| Agents | `/agents`, `/agents/[id]` | `GET /agents`, `/agents/{id}`, `/agents/{id}/versions`, `/evaluations?agent_version_id=`, `/evaluations/{id}/summary` |
| Tasks | `/tasks` | `GET /tasks?dataset_id=&task_type=&difficulty=`, `/datasets` |
| Evaluations | `/evaluations`, `/evaluations/[id]` | `GET /evaluations`, `/evaluations/{id}`, `/summary`, `/results`; `POST /evaluations/{id}/cancel` |
| Compare | `/evaluations/compare?ids=a,b,c` | `GET /evaluations/compare?ids=` (2 to 5, same dataset and scoring hash) |
| Runs | `/runs` (also `?status=&dead_letter=true&evaluation_id=&task_id=`) | `GET /runs` |
| Run detail | `/runs/[id]` | `GET /runs/{id}`, `/attempts`, `/attempts/{n}/events` (paged by `after_seq`), `/attempts/{n}/evaluation`; `POST /runs/{id}/cancel`, `/replay` |
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
components/ui.tsx    badges, panels, tables, loading / error / empty states
lib/api/types.ts     hand-written types mirroring the backend's public schemas
lib/api/client.ts    typed fetch client (ApiError, problem+json parsing, one function per endpoint)
lib/hooks.ts         query and mutation hooks (live refresh while runs are in progress)
lib/format.ts        durations, cost, percentages, timestamps
tests/               vitest + Testing Library with a mocked fetch
```

Design: dark by default, light via `prefers-color-scheme`; monospace for ids and numbers; status colours are defined once (`lib/status.ts`, CSS variables) and shared by badges and charts. No animations.

## Checks

```bash
npm run typecheck && npm run lint && npm test && npm run build
```

Dependencies are pinned to exact versions; `npm audit` reports advisories in the dev-only lint toolchain (`eslint-config-next` -> `fast-glob` -> `braces`); none ship in the runtime bundle.
