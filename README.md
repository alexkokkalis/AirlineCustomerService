# Ionian Airlines Customer-Service Agent

An assessment project for a policy-aware airline customer-service agent. The system combines an ElevenLabs conversational agent (**Erling**), a local FastAPI booking API, an OpenAI-powered customer simulator and reviewer, and a bounded autonomous refinement loop.

It supports policy questions, flight and seat search, verified booking retrieval, booking creation, ancillary purchases, cancellation, and rescheduling. Every agent-facing action is recorded in a structured, run-scoped log so that simulated conversations can be evaluated and refined with evidence.

## First run: from clone to dashboard

Follow these steps in order from a terminal. They are intended to be enough for a new developer to run the complete local system.

### 1. Clone the repository and enter it

```bash
git clone https://github.com/alexkokkalis/AirlineCustomerService.git
cd AirlineCustomerService
```

### 2. Create and activate the Python environment

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

The `source` command must be repeated in each new terminal before running Python commands directly. A successful activation usually adds `(.venv)` to the shell prompt.

### 3. Add the separately provided local credentials

Place the separately provided `keys.env` file in the repository root. It is ignored by Git and must never be committed.

```dotenv
IONIAN_TOOL_TOKEN=replace-with-a-long-random-shared-token
ELEVENLABS_API_KEY=...
ELEVENLABS_AGENT_ID=...
OPENAI_API_KEY=...
```

### 4. Choose the appropriate Git branch

For a safe dashboard run or plan-only refinement, any branch is fine. For an apply-capable refinement, the code applier will only operate on the dedicated branch:

```bash
git switch autonomous-refinement
```

Do this only with a clean working tree. Prompt changes always target the configured dedicated ElevenLabs refinement branch; the live/main ElevenLabs agent is never targeted by the Applier.

### 5. Start everything

```bash
bash scripts/run_local.sh
```

The launcher creates the sandbox database if it is missing, starts FastAPI, waits for its health check, and starts ngrok using the configured stable hostname. Keep this terminal open.

### 6. Open the dashboard

Open this URL in a browser:

```text
http://127.0.0.1:8000/dashboard
```

### 7. Use the dashboard controls

| Control | Meaning |
| --- | --- |
| **Scenario dropdown** | Chooses the customer goal, fixtures, expected tools, and deterministic checks to run. |
| **Run dry simulation** | Runs the scenario, deterministic evaluation, LLM review, and Refiner planning. It does **not** apply a prompt or source-code refinement. The simulated scenario may still create, change, or cancel sandbox booking data as part of its test. |
| **Apply validated refinement** checkbox | Enables an apply-capable loop after a browser confirmation. A validated prompt plan can update only the dedicated ElevenLabs refinement branch; a validated code plan can update only allowlisted files on `autonomous-refinement`. |
| **Run refinement loop** | Appears when the checkbox is enabled and starts the bounded simulate → evaluate → plan → apply → verify loop. |

The four panels show the conversation, evaluation, refinement plan, and exact diff. The launcher also prints the public webhook endpoint and the hosted [ngrok Traffic Inspector](https://dashboard.ngrok.com/traffic-inspector). Press `Ctrl-C` in the launcher terminal to stop Uvicorn and ngrok.

## Architecture

```text
OpenAI customer simulator ──text turns──> ElevenLabs Erling
                                            │
                                            │ authenticated webhooks
                                            v
                                      FastAPI / SQLite
                                            │
                            JSONL transcript + tool audit events
                                            │
                         deterministic evaluator + OpenAI reviewer
                                            │
                                  Refiner plan + guarded applier
                                            │
                         dedicated ElevenLabs branch / allowlisted code
```

The local monitoring dashboard is served by the same FastAPI application. It uses Server-Sent Events (SSE) to render new transcript, evaluation, refinement, and diff events while a job is running.

## Main components

| Component | Responsibility |
| --- | --- |
| `app/ionian_api.py` | Authenticated FastAPI webhook API, policy access, and SQLite booking lifecycle. |
| `app/elevenlabs_chat.py` | Text-mode ElevenLabs Chat Mode session adapter used by the simulator. |
| `app/simulation_runner.py` | Runs one scenario turn-by-turn between the customer simulator and Erling. |
| `app/customer_simulator.py` | OpenAI customer agent that follows a scenario goal and chooses `message` or `end`. |
| `app/evaluation.py` | Deterministic, evidence-backed checks over the transcript and tool audit. |
| `app/llm_evaluator.py` | Independent OpenAI reviewer that scores quality and classifies failures. |
| `app/refiner.py` | Produces one structured refinement plan from a failed run. |
| `app/refinement_applier.py` | Applies only validated, exact prompt/code changes within strict scope. |
| `app/refinement_runner.py` | Bounded `simulate → evaluate → plan → apply → verify` orchestration. |
| `app/dashboard_api.py` | Development dashboard API, SSE event stream, and static dashboard. |
| `data/scenarios.json` | Version-controlled simulation scenarios, expected tools, success criteria, and fixtures. |

## Prerequisites

- Python 3.11+ (developed with Python 3.13)
- An ElevenLabs account with an agent configured for text-mode Chat Mode and webhook tools
- An OpenAI API key for the customer simulator, evaluator, and refiner
- [ngrok](https://ngrok.com/) or another HTTPS tunnel when ElevenLabs needs to call the local API

## Configuration and reset reference

The OpenAI model choices and refinement guardrail limits are version-controlled application settings in `app/config.py` and `app/guardrails.py`; they are deliberately not secrets. The simulator supplies an `X-Ionian-Run-ID` dynamic header, which links tool calls to the correct transcript.

> **Reset warning:** `python scripts/init_database.py` recreates the SQLite database and removes sandbox bookings. It is useful for reproducible test data, not for production data.

## Agent tools and API

All agent-callable routes require `X-Ionian-Tool-Token` when `IONIAN_TOOL_TOKEN` is configured.

| Agent capability | HTTP route | Purpose |
| --- | --- | --- |
| `get_policy` | `GET /policies/{topic}` | Retrieves one authoritative policy topic. |
| `search_flights` | `GET /flights` | Searches scheduled flights by destination, date, fare tier, and/or budget. |
| `get_available_seats` | `GET /flights/{flight_id}/seats` | Returns currently available seats for a flight. |
| `get_booking` | `GET /bookings/{reference}?contact_email=...` | Returns verified booking, segment, ancillary, assistance, and refund information. |
| `create_booking` | `POST /bookings` | Atomically creates a booking and reserves an available seat. |
| `add_ancillary` | `POST /bookings/{reference}/ancillaries?contact_email=...` | Adds a policy-priced checked bag, pet reservation, or special item. |
| `cancel_booking` | `POST /bookings/{reference}/cancel?contact_email=...` | Cancels confirmed segments and returns the resulting refund/travel-credit outcome. |
| `quote_reschedule` | `POST /bookings/{reference}/reschedule/quote?contact_email=...` | Validates a prospective change without mutating the booking. |
| `reschedule_booking` | `POST /bookings/{reference}/reschedule?contact_email=...` | Applies a quoted, confirmed change to flight and seat. |

`get_booking` uses the booking reference and primary contact email as two backend-validated factors. The API does not reveal which factor was incorrect.

## Running simulations

`scripts/sandbox.py` is a deliberately explicit development workbench. Uncomment one helper in `main()` and run it from the project root:

```bash
.venv/bin/python scripts/sandbox.py
```

Useful helpers include:

```python
simulate_scenario("pet_policy_in_cabin")
simulate_assessment_scenario("reschedule_existing_booking")
simulate_refinement_verification("create_business_booking")
run_refinement_loop("reschedule_existing_booking", apply_changes=False)
```

- `simulate_scenario` runs a paid ElevenLabs + OpenAI conversation and deterministic evaluation.
- `simulate_assessment_scenario` additionally runs the OpenAI LLM reviewer.
- Fixture-backed scenarios create their own isolated booking immediately before the conversation; fixtures are not permanent scenario data.
- `apply_changes=False` validates and displays a refinement plan without changing a prompt or source file.

For a live dashboard run, choose a scenario at `/dashboard`. The default **dry simulation** mode does not mutate prompts or source. Enabling the refinement checkbox asks for a browser confirmation before starting an apply-capable loop.

## Evaluation and refinement

Each completed scenario is evaluated twice:

1. **Deterministic evaluator** — verifies terminal outcome, expected/forbidden tools, recoverable vs unrecoverable tool errors, duplicate mutations, and action-specific ordering rules. It uses transcript and API-audit facts only.
2. **LLM evaluator** — scores request understanding, API usage, outcome confirmation, and natural end-to-end quality from 1–10. It receives the deterministic report as mandatory evidence and classifies each material failure as `prompt_issue`, `code_issue`, or (only when independently proven) `mixed`.

The Refiner receives the transcript, both evaluations, scenario context, and—when relevant—the current prompt from the dedicated ElevenLabs refinement branch. It returns exactly one plan for the next iteration. The Applier enforces these safeguards:

- prompt edits target a dedicated non-main ElevenLabs branch;
- local code edits are permitted only on the Git branch `autonomous-refinement`;
- local edits are restricted to a small allowlist of simulation, evaluation, API, and fixture files;
- the proposed text must have one exact match before replacement;
- iterations, scenarios, turns, tool calls, and provider calls are capped in `app/guardrails.py`.

The applier never automatically commits or pushes Git changes, and it never promotes a refinement-branch prompt to the live/main ElevenLabs agent.

## Logs and evidence

Generated logs are intentionally ignored by Git because they may contain sandbox personal data or provider identifiers.

| Location | Contents |
| --- | --- |
| `logs/runs/<run_id>.jsonl` | Ordered customer/agent messages, lifecycle events, safe tool request metadata, and iteration events. |
| `logs/evaluations/<run_id>.json` | Deterministic report. |
| `logs/evaluations/<run_id>.llm.json` | LLM scores, evidence quotes, root-cause classification, and decision. |
| `logs/refinements/<run_id>.plan.json` | Refiner’s proposed single change and rationale. |
| `logs/refinements/<run_id>.applied.json` | Applied-change audit, including before/after digests and unified diff when a change is made. |
| `logs/api_events.jsonl` | Operational API request metadata. |
| `logs/agent_events.jsonl` | ElevenLabs session operational metadata. |

The dashboard can replay a persisted run with:

```text
/dashboard?run_id=run_<id>
```

Live dashboard job IDs are process-local. A Uvicorn reload clears the active job registry, but never removes the durable run, evaluation, or refinement files above.

A redacted, Git-tracked example of the required pipeline evidence is available in [deliverables/recorded-example-reschedule.md](deliverables/recorded-example-reschedule.md) and [deliverables/pipeline-summary-reschedule-example.json](deliverables/pipeline-summary-reschedule-example.json). The unredacted runtime version is generated automatically for each refinement loop at `logs/pipeline_summaries/<pipeline_id>.json`.

## Trade-offs

- **SQLite and local tunnel:** fast, reproducible assessment development rather than a production deployment architecture.
- **Synchronous, single-process dashboard jobs:** simple observability with SSE; active dashboard job state does not survive a server restart.
- **Bounded autonomous changes:** deliberately favors safe, inspectable refinement over unrestricted repository access or automatic deployment.
- **LLM simulation variability:** scenarios constrain goals and expected tools, but independent model turns retain realistic variation. Deterministic checks provide stable evidence alongside LLM judgment.
- **Policy data is local:** the knowledge base is an authoritative project fixture, not a live airline policy system.

## Improvements to pursue next

- Persist dashboard job metadata so job-to-run relationships survive API restarts.
- Add unit/integration tests for evaluators, API endpoints, and refinement-plan validation.
- Add a CI workflow to run static checks and deterministic scenario fixtures.
- Add idempotency keys and stronger authentication/identity controls for state-changing production APIs.
- Move SQLite, local JSONL logs, and in-process jobs to managed database, object storage, and queue/worker infrastructure.
- Add role-based dashboard access, retention policies, and structured privacy redaction appropriate for real customer data.
- Promote verified refinement-branch changes through a reviewed release workflow rather than manually.

## Repository safety

Do not commit `keys.env`, generated logs, the SQLite database, or virtual environments. Before using `apply_changes=True`, switch to the dedicated Git branch:

```bash
git switch autonomous-refinement
git merge --ff-only main
```

Then inspect the generated plan and applied diff before deciding whether to commit the resulting source change.
