# AI Agent Eval

Runs an AI agent many times inside controlled environments, grades the resulting state, and compares configurations task by task.

Status: real model agents run end to end, with costs tracked and capped. A scripted agent or a model (Gemini, or a local Ollama model) handles a refund task in the mock retail store, every model call and tool call is traced, and the result is graded from the store's final state. People sign in, work in separate workspaces, and use API tokens for scripts. The job queue that runs experiments in the background, and the web pages, come next.

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

Some runs are left unscored instead of failed, because the agent did nothing wrong: the model could not be reached even after retries, or the experiment ran out of budget mid-run. Budget-stopped runs are retried from scratch when the experiment resumes.

## Trace events

| Event | When |
| --- | --- |
| `attempt_started` | The agent gets its input. Holds what the agent saw, its tools and its limits |
| `tool_call_requested` | The agent asked for a tool. Recorded before anything runs |
| `tool_call_completed` | The gateway answered. Holds the outcome and any state change |
| `model_call` | One request to a model, including each retry. Holds the reply, tool calls, token usage and cost |
| `agent_finished` | The agent stopped. Holds the final message and why it stopped |

## Grading

| Evaluator | Dimension | Reason codes |
| --- | --- | --- |
| `refund_outcome` 1.0.0 | `refund_amount` | `MISSING_REFUND`, `WRONG_REFUND_AMOUNT`, `DUPLICATE_REFUND` |
| `refund_outcome` 1.0.0 | `unexpected_refunds` | `UNEXPECTED_REFUND` |

If an evaluator crashes, its score is `evaluation_error`. That never counts as a pass or as an agent failure.

## Environments

`support-retail@1` is a made-up electronics store. Its tools:

| Tool | What it does |
| --- | --- |
| `get_order` | An order's status, items and amounts |
| `get_return_status` | Whether the returned item arrived and passed inspection |
| `get_refund_status` | Refunds already issued and how much is still refundable |
| `search_policy` | Searches the store's support policies |
| `request_refund_approval` | Asks a simulated reviewer to approve an exact refund amount |
| `issue_refund` | Refunds an order, only with an approved approval for that order and amount |
| `update_ticket` | Sets the customer's ticket status and adds a note |

The store's six business rules, and who enforces each:

| Rule | Enforced by the store | Checked by evaluators |
| --- | --- | --- |
| A customer can only access their own orders and tickets | Yes | Yes |
| A refund needs a return that arrived and passed inspection | Through the reviewer | Yes |
| Every refund needs an approval | Yes | Yes |
| A refund cannot exceed what is left on the order | Yes | Yes |
| A repeated request must not pay twice | Yes, one approval per order and each approval used once | Yes |
| A ticket is not resolved while a refund's outcome is unknown | No. The store cannot know what the agent saw | Yes |

The reviewer is a simulator. Each task says which order and maximum amount it may approve, and whether it approves, rejects or lets the request expire. Approval records are marked `simulated`.

Money in the retail store is in minor units (cents or paise), stored as integers.

## Quick start

You need Docker and [uv](https://docs.astral.sh/uv/).

```bash
cp .env.example .env
make up
curl http://127.0.0.1:8000/health/ready
```

`make up` starts Postgres, runs migrations, then starts the API and the worker. To store provider keys, first set `APP_MASTER_KEY` in `.env`; the comment above it shows how to make one.

On a fresh install, create the first account. That person becomes the owner of the first workspace. This works only once.

```bash
curl -X POST http://127.0.0.1:8000/api/v1/auth/setup -H 'content-type: application/json' -d '{"email": "you@example.com", "password": "at least twelve characters", "workspace_name": "My team"}'
```

Then sign in with `POST /api/v1/auth/login`, or create an API token for scripts.

Experiments can be created through the API, but nothing runs them in the background yet. The job queue and worker that do arrive in a later step. Until then, experiments run from the tests, including the real Gemini test below.

To see the end-to-end refund path run, use `make test`. The tests run a correct script and a script that refunds the wrong amount, and check that the second one fails with `WRONG_REFUND_AMOUNT` pointing at the refund call.

Demo mode with scripted agents and no provider key is not built yet.

## Models, keys and money

| Agent type | What it is | Needs |
| --- | --- | --- |
| `scripted` | Replays fixed tool calls. Only allowed in `demo` experiments | Nothing |
| `tool_loop` with `gemini` | Our own agent loop on Gemini | A Gemini key stored in the workspace |
| `tool_loop` with `ollama` | The same loop on a local model. No data leaves the machine | Ollama running at `OLLAMA_BASE_URL` |

A `tool_loop` config looks like `{"provider": "gemini", "model": "gemini-flash-lite-latest"}`, plus an optional `system_prompt`, `temperature` and `max_output_tokens`.

**Provider keys.** An owner stores a key with `POST /api/v1/provider-credentials`. It is encrypted with AES-GCM using `APP_MASTER_KEY`, and only its label and last four characters are ever shown. Keys never appear in traces, logs or errors; log lines are scrubbed of anything shaped like a secret. A key can carry `requests_per_minute` and `tokens_per_minute`, and every worker using that key shares one rate limit stored in Postgres. Launching a real experiment that needs a key the workspace does not have returns `409 provider_key_required`.

Free-tier Gemini traffic may be used by Google to improve its products, so send only synthetic data through a free-tier key.

**Prices.** An owner adds a price for a model with `POST /api/v1/price-versions`, per million tokens, in micro-units (1 USD is 1,000,000 micros). The newest price is frozen into each experiment when it is created, so later price changes never rewrite old costs. A money cap needs a price; without one, launching returns `422 price_unknown`.

**Budgets.** Every experiment has a budget with optional caps on money, tokens and requests. Before each model call the worst case is reserved in one atomic update, so two workers can never both spend the last of a budget. After the call the reservation is settled to what was really used. If the provider does not report usage, the reserved amount stands, because unknown cost is never zero.

```mermaid
flowchart LR
    R[Reserve worst case] -->|fits| C[Call the model]
    R -->|does not fit| P[Pause: paused_budget]
    C --> S[Settle to real usage]
    S -->|80% of a cap| W[Warn once]
    S -->|next call would not fit| P
    P --> U[Raise the cap and resume, or close as partial]
```

Pausing never deletes anything. Finished runs stay as they are. Raise the cap with `PATCH /api/v1/experiments/{id}/budget`, then `POST .../resume`, or end it with `POST .../close` as `closed_partial`.

Rate-limited calls (`429`) are retried with backoff and cost nothing. Other transient errors are retried too, and keep their reserved cost. Every try is recorded.

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
| `POST /api/v1/experiments/{id}/pause`, `/resume`, `/close` | Pause, resume, or close a paused experiment as partial | member |
| `PATCH /api/v1/experiments/{id}/budget` | Change the caps. Never below what is used | owner |
| `GET /api/v1/runs/{id}`, `GET /api/v1/attempts/{id}/events` | A run with its scores, the ordered trace of an attempt | viewer |
| `GET`, `POST /api/v1/provider-credentials`, `DELETE .../{id}` | Manage encrypted provider keys | owner |
| `GET`, `POST /api/v1/price-versions` | List and add model prices | viewer, owner |
| `GET /health/live`, `GET /health/ready` | Process is up; database is reachable and migrated | none |

Scripted agents can only run in `demo` experiments, and demo experiments can only run scripted agents. Canned answers never show up as real results, and demos never spend money.

## Make commands

| Command | What it does |
| --- | --- |
| `make up` | Build and start the stack, wait until healthy |
| `make down` | Stop the stack |
| `make logs` | Follow logs from every service |
| `make ps` | Show service status |
| `make migrate` | Run database migrations |
| `make db` | Start only Postgres, for running tests on the host |
| `make test` | Start Postgres, migrate, run the tests. Needs no network or key |
| `make test-paid` | Run the one test that calls the real Gemini API. Needs `GEMINI_API_KEY` |
| `make lint` | Check lint and formatting |
| `make format` | Fix formatting and safe lint issues |
| `make typecheck` | Run mypy in strict mode on `app/` |
| `make check` | Lint, typecheck and test |

## Folders

| Folder | What it holds |
| --- | --- |
| `app/agents/` | The agent contract, the scripted agent and the built-in tool loop |
| `app/api/` | HTTP routes, including health checks |
| `app/auth/` | Accounts, passwords, sessions, invites and API tokens |
| `app/budget/` | Prices, worst-case estimates, reservations, warnings and pausing |
| `app/db/` | Tables, the workspace-scoped data layer, datasets, experiments, audit log |
| `app/environments/` | The environment contract, its state store and the retail store |
| `app/evaluators/` | The evaluator contract and the checks that grade runs |
| `app/execution/` | The attempt and experiment runners, launch checks, the trace recorder, the worker |
| `app/gateway/` | The tool gateway every agent calls |
| `app/providers/` | Model providers (Gemini, Ollama), encrypted keys, rate limiting, metered calls |
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
| `APP_MASTER_KEY` | none | Encrypts stored provider keys. Base64 of 32 random bytes. Needed to store or use a key |
| `OLLAMA_BASE_URL` | `http://localhost:11434` | Where a local Ollama server listens |
| `MODEL_CALL_TIMEOUT_SECONDS` | `60` | How long one model request may take |
| `MODEL_CALL_MAX_RETRIES` | `4` | Retries for rate-limited or failed model requests |

## Connect your own agent

Not built yet. External agents will connect over HTTP and call the tool gateway with a short-lived run token. A minimal example agent will live in `examples/`.

## License

No license has been chosen yet. Until then, all rights are reserved.
