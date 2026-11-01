# AI Agent Eval

Runs an AI agent many times inside controlled environments, grades the resulting state, and compares configurations task by task.

Status: one complete path works end to end, behind accounts. A scripted agent handles a refund task in the mock retail store, every tool call is traced, and the result is graded from the store's final state. People sign in, work in separate workspaces, and use API tokens for scripts. Real model agents, the job queue that runs experiments, and the web pages come next.

## How a run flows

```mermaid
flowchart LR
    E[Experiment] --> W[Worker]
    W --> A[Agent]
    A -->|tool calls| G[Tool gateway]
    G --> V[Environment]
    V -->|final state| X[Evaluators]
    W -->|trace| X
    X --> R[Report]
```

Every agent changes state only through the tool gateway. Evaluators read the hidden expected state, which the agent never sees.

## What happens in one attempt

1. The runner creates fresh state for the task's environment from the task's hidden fixture, and saves an initial snapshot.
2. The agent gets only the agent-visible part of the task: the customer's message and who is signed in.
3. Every tool call goes through the gateway. It checks the tool is allowed, validates the arguments strictly, takes the customer's identity from the server (never from the agent), runs the tool, and records the call and its effect in the trace.
4. When the agent stops, the runner saves a final snapshot and removes the live state.
5. Evaluators compare the final state with what the task expects. Each failure names a reason code and points at the trace events that caused it.

A run passes only if every applicable check passes and the agent did not hit a hard limit (turns, tool calls or time).

## Trace events

| Event | When |
| --- | --- |
| `attempt_started` | The agent gets its input. Holds what the agent saw, its tools and its limits |
| `tool_call_requested` | The agent asked for a tool. Recorded before anything runs |
| `tool_call_completed` | The gateway answered. Holds the outcome and any state change |
| `agent_finished` | The agent stopped. Holds the final message and why it stopped |

## Grading

| Evaluator | Dimension | Reason codes |
| --- | --- | --- |
| `refund_outcome` 1.0.0 | `refund_amount` | `MISSING_REFUND`, `WRONG_REFUND_AMOUNT`, `DUPLICATE_REFUND` |
| `refund_outcome` 1.0.0 | `unexpected_refunds` | `UNEXPECTED_REFUND` |

If an evaluator crashes, its score is `evaluation_error`. That never counts as a pass or as an agent failure.

## Environments

| Environment | Tools | Rules enforced today |
| --- | --- | --- |
| `support-retail@1` | `get_order`, `issue_refund` | A customer only sees and refunds their own orders. Refunds cannot exceed what is left on the order |

Money in the retail store is in minor units (cents), stored as integers.

## Quick start

You need Docker and [uv](https://docs.astral.sh/uv/).

```bash
cp .env.example .env
make up
curl http://127.0.0.1:8000/health/ready
```

`make up` starts Postgres, runs migrations, then starts the API and the worker.

On a fresh install, create the first account. That person becomes the owner of the first workspace. This works only once.

```bash
curl -X POST http://127.0.0.1:8000/api/v1/auth/setup -H 'content-type: application/json' -d '{"email": "you@example.com", "password": "at least twelve characters", "workspace_name": "My team"}'
```

Then sign in with `POST /api/v1/auth/login`, or create an API token for scripts.

Experiments can be created through the API, but nothing runs them yet. The job queue and worker that run them arrive in a later step.

To see the end-to-end refund path run, use `make test`. The tests run a correct script and a script that refunds the wrong amount, and check that the second one fails with `WRONG_REFUND_AMOUNT` pointing at the refund call.

Demo mode with scripted agents and no provider key is not built yet.

## Accounts and workspaces

Every workspace is sealed off from the others. Every row in the database carries its workspace, every query is filtered by it, and a row in another workspace answers exactly like a row that does not exist (`404`).

| Role | Can do |
| --- | --- |
| `viewer` | Read everything in the workspace |
| `member` | Also import datasets, register agent configs, create experiments |
| `owner` | Also invite people and manage API tokens |

| How you call the API | How it works |
| --- | --- |
| Signed in (browser) | `POST /api/v1/auth/login` sets an HTTP-only cookie and returns a `csrf_token`. Any request that changes something must send it back in the `X-CSRF-Token` header |
| API token (scripts, CI) | Send `Authorization: Bearer aet_...`. A token belongs to one workspace and one role. Its secret is shown once, only its hash is kept, and it can be revoked |

Owners invite people with `POST /api/v1/workspaces/{id}/invites`, which returns a one-time token. The invited person joins with `POST /api/v1/invites/accept`.

Creating a workspace, inviting, joining, creating or revoking a token, importing a dataset, registering a config and creating an experiment are all written to the audit log.

## API

| Route | What it does | Role |
| --- | --- | --- |
| `POST /api/v1/auth/setup` | Create the first owner on a fresh install | none |
| `POST /api/v1/auth/login`, `POST /api/v1/auth/logout` | Start or end a session | none, then signed in |
| `GET /api/v1/workspaces`, `POST /api/v1/workspaces` | List your workspaces, create one | viewer, signed in |
| `POST /api/v1/workspaces/{id}/invites`, `POST /api/v1/invites/accept` | Invite someone, join with an invite | owner, none |
| `GET`, `POST /api/v1/api-tokens`, `DELETE /api/v1/api-tokens/{id}` | Manage API tokens | owner |
| `POST /api/v1/datasets/import`, `GET /api/v1/datasets` | Import tasks as a new dataset version, list datasets | member, viewer |
| `POST /api/v1/agent-configs`, `GET /api/v1/agent-configs` | Register an agent config, list them | member, viewer |
| `POST /api/v1/experiments`, `GET /api/v1/experiments`, `GET /api/v1/experiments/{id}` | Create an experiment (send `Idempotency-Key` to make retries safe), list, view | member, viewer |
| `GET /api/v1/experiments/{id}/runs` | Runs of an experiment, filtered by `evaluation_status`, paginated | viewer |
| `GET /api/v1/runs/{id}`, `GET /api/v1/attempts/{id}/events` | A run with its scores, the ordered trace of an attempt | viewer |
| `GET /health/live`, `GET /health/ready` | Process is up; database is reachable and migrated | none |

Scripted agents can only run in `demo` experiments, so canned answers never show up as real results.

## Make commands

| Command | What it does |
| --- | --- |
| `make up` | Build and start the stack, wait until healthy |
| `make down` | Stop the stack |
| `make logs` | Follow logs from every service |
| `make ps` | Show service status |
| `make migrate` | Run database migrations |
| `make db` | Start only Postgres, for running tests on the host |
| `make test` | Start Postgres, migrate, run the tests |
| `make lint` | Check lint and formatting |
| `make format` | Fix formatting and safe lint issues |
| `make typecheck` | Run mypy in strict mode on `app/` |
| `make check` | Lint, typecheck and test |

## Folders

| Folder | What it holds |
| --- | --- |
| `app/agents/` | The agent contract and the scripted agent |
| `app/api/` | HTTP routes, including health checks |
| `app/auth/` | Accounts, passwords, sessions, invites and API tokens |
| `app/db/` | Tables, the workspace-scoped data layer, datasets, experiments, audit log |
| `app/environments/` | The environment contract, its state store and the retail store |
| `app/evaluators/` | The evaluator contract and the checks that grade runs |
| `app/execution/` | The attempt runner, the trace recorder and the worker |
| `app/gateway/` | The tool gateway every agent calls |
| `datasets/` | Tasks, one JSON file each |
| `migrations/` | Alembic migrations |
| `tests/` | Tests, laid out like `app/` |

## Settings

All settings are in `.env.example`. The main ones:

| Setting | Default | Meaning |
| --- | --- | --- |
| `DATABASE_URL` | local Postgres | Database for running outside Docker. Compose sets its own |
| `API_PORT` | `8000` | Host port for the API |
| `POSTGRES_PORT` | `5432` | Host port for Postgres |
| `LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING` or `ERROR` |
| `LOG_FORMAT` | `json` | `json` or `text` |
| `SESSION_TTL_HOURS` | `12` | How long a sign-in lasts |
| `SESSION_COOKIE_SECURE` | `false` | Send the session cookie over HTTPS only. Turn on anywhere but a laptop |
| `INVITE_TTL_HOURS` | `72` | How long an invite stays valid |

## Connect your own agent

Not built yet. External agents will connect over HTTP and call the tool gateway with a short-lived run token. A minimal example agent will live in `examples/`.

## License

No license has been chosen yet. Until then, all rights are reserved.
