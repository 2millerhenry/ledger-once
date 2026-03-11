# Ledger Once

[![PyPI](https://img.shields.io/pypi/v/ledger-once)](https://pypi.org/project/ledger-once/)

**Exactly-once execution for AI agent tool calls and retries.**

Ledger turns side-effecting tools into exactly-once operations.

Your agent will retry. Ledger guarantees the action only happens once. Designed for production: safe across crashes, retries, and concurrent workers.

A quick decorator gets you 80% there. The other 20% is where production bugs live — race conditions, arg normalization, crash recovery, float drift. Ledger handles all of it in one line.

```bash
pip install ledger-once
# on Mac: pip3 install ledger-once
```

Ledger turns side-effecting tools into exactly-once operations. Retry as aggressively as you want — duplicate actions are impossible.

---

## The problem in one line

AI agents retry tools. When those tools have side effects, retries duplicate real actions.

```python
# Without Ledger                        # With Ledger

if not already_processed(order_id):     guard(charge_customer, order_id=123)
    charge_customer(order_id)
    mark_processed(order_id)
```

One call. No bookkeeping. No duplicate charges.

![Ledger demo](Docs/LEDGERGIF-ezgif.com-video-to-gif-converter.gif)

---

## Stop rewriting idempotency for every tool

Without a shared guard, every side-effecting tool ends up with its own protection logic:

```python
if not already_processed(order_id):
    charge_customer(order_id)
    mark_processed(order_id)
```

Then the same pattern appears for `send_email`, `refund_payment`, `create_invoice`, `trigger_webhook` — each one reimplementing its own protection in its own way.

Ledger centralizes it entirely:

```python
guard(charge_customer, order_id=123)
guard(send_email, to="user@example.com")
guard(trigger_webhook, url=webhook_url)
```

Or protect your entire toolset at once:

```python
tools = guard.wrap_tools(tools)
```

One system. One policy. One place to reason about retries. Every tool call automatically protected — no per-tool logic needed.

---

## Small enough to trust

Ledger is intentionally minimal:

- Single file implementation
- Zero dependencies
- SQLite by default — no infrastructure needed
- Easy to audit — read the entire implementation in minutes

```bash
cp ledger.py your_project/   # copying the file directly is a valid install method
```

---

## Protect your entire toolset in one line

```python
from ledger import guard

tools = guard.wrap_tools(tools)  # dict or list — every tool auto-protected
```

Every tool now runs **at most once per unique argument set**, even across restarts, crashes, and concurrent workers. Your agent can retry as aggressively as it wants.

---

## Or protect a single call

```python
from ledger import guard

guard(stripe_charge, customer="cus_42", amount=99)   # 💳 charged — runs     ✓
guard(stripe_charge, customer="cus_42", amount=99)   # blocked  ✗
guard(stripe_charge, customer="cus_42", amount=99)   # blocked  ✗
```

---

## Real failures this prevents

Duplicate Stripe charges. Duplicate refunds. Duplicate emails.
Duplicate webhooks. Duplicate database writes.

If your agent calls external APIs, you already have this risk.

---

## How it works

```
Agent calls tool
       │
       ▼
  Ledger guard
       │
 ┌─────┴──────┐
 │ fingerprint │
 │ + database  │
 └─────┬──────┘
       │
 ran before?
  YES → block
  NO  → run and record
```

Ledger fingerprints every call using `(tool_name, args, workflow)`, claims it atomically in SQLite, and blocks any duplicate that arrives after. Records survive restarts — no configuration needed.

---

## Guarantees

- **Duplicate calls are always blocked** — same args + same workflow = same fingerprint, always
- **Records persist across restarts** — SQLite file survives process death, no setup needed
- **Concurrent workers cannot execute the same tool twice** — atomic `INSERT OR IGNORE` at the database level
- **Crashed processes recover** — stale `RUNNING` records expire after a configurable timeout (default 300s)
- **Failed tools always retry** — `FAILED` records auto-clear so broken tools are never permanently stuck

---

## See exactly what your agent is doing

Ledger records every tool attempt, execution, and blocked duplicate.

```python
guard.log()
# ✓ send_email    attempts 3   executed 1   blocked 2
# ✗ stripe_charge attempts 1   executed 0   → CardError: declined
```

Or inspect it live:

```bash
ledger-dashboard ledger.db
```

See every tool call in real time — what executed, what was blocked, and which workflow triggered it. When something looks wrong, you'll know immediately.

---

## Edge cases handled automatically

**Argument order doesn't matter**
```python
guard(fn, x=1, y=2)
guard(fn, y=2, x=1)   # same fingerprint — blocked
```

**Float drift is handled**
```python
guard(fn, amount=99.99)
guard(fn, amount=99.9900000001)   # same fingerprint — blocked
```

**Non-deterministic args (timestamps, UUIDs)**
```python
# ✗ BAD — timestamp changes every call, Ledger can't deduplicate
guard(send_email, to="user@x.com", sent_at=datetime.now())

# ✓ GOOD — stable key, non-deterministic args ignored
guard(send_email, to="user@x.com", sent_at=datetime.now(), key=f"email-{order_id}")
```

Ledger detects this automatically and warns you:
```
[ledger] ⚠  send_email called 4× with different args in <10s — if retrying, add key=
```

**Crash recovery**
```
process dies mid-execution
RUNNING record → expires after timeout → next call retries cleanly
```

---

## When to use this

Useful any time a tool:

- Charges payments
- Sends emails or notifications
- Triggers webhooks
- Modifies a database
- Calls any external API with side effects

If your tool is read-only, mark it unlimited and it always runs:
```python
guard.policy(search_web, unlimited=True)
```

---

## Works with every framework

### OpenAI

```python
from ledger import guard

tool_map = guard.wrap_tools(
    {"send_email": send_email, "charge_card": stripe_charge},
    blocked_return={"status": "blocked"},  # no None check needed in dispatch
)

def dispatch_tool(name: str, arguments: dict) -> str:
    result = tool_map[name](**arguments)
    return json.dumps(result)
```

A runnable example is in `examples/example_openai.py`.

---

### LangChain

```python
from ledger import guard
from langchain_core.tools import StructuredTool

# Wrap raw functions BEFORE StructuredTool — not after
protected = guard.wrap_tools([send_email, stripe_charge])
agent_tools = [StructuredTool.from_function(fn) for fn in protected]
```

A runnable example is in `examples/example_langchain.py`.

---

### Any framework

```python
tools = guard.wrap_tools([search_web, send_email, stripe_charge, create_ticket])
agent = YourAgent(tools=tools)
```

---

## Per-tool policies

```python
guard.policy(search_web,    unlimited=True)  # read-only: always run
guard.policy(stripe_charge, replay=True)     # blocked callers get the cached result
guard.policy(send_sms,      max=2)           # allow up to 2 executions
guard.policy(daily_report,  ttl=86400)       # once per day — reset after 24h
```

---

## Async

Works automatically — no extra syntax.

```python
result = await guard(post_webhook, url="...", payload=data)
```

---

## Decorator

```python
@guard.once
def stripe_charge(card_id, amount): ...

@guard.once(replay=True)
def create_invoice(id, amount): ...
```

---

## Escape hatches

```python
guard.retry(send_email, to="user@example.com")  # clear record → next call executes
guard.force(send_email, to="user@example.com")  # execute immediately, bypass all checks
```

---

## Observability

```python
guard.log()
# ✓ send_email    attempts 3   executed 1   blocked 2
# ✗ stripe_charge attempts 1   executed 0   → CardError: declined

guard.stats()
# {'actions': 2, 'attempts': 4, 'executed': 1, 'blocked': 2, 'failed': 1}

guard.history()                  # list[dict] of all records
guard.history(tool=send_email)   # filter by tool
guard.history(wf="order-42")     # filter by workflow
```

---

## Metrics hooks

```python
guard.on_success(lambda r: metrics.increment("ledger.executed", tags={"tool": r.tool}))
guard.on_block(lambda r:   metrics.increment("ledger.blocked",  tags={"tool": r.tool}))
```

---

## Verify it's working

```python
assert guard.check()   # runs a self-test in memory — raises if something is wrong
```

---

## CLI

```bash
ledger show  ledger.db [--wf WORKFLOW]         # full history table
ledger tail  ledger.db [--wf WORKFLOW]         # live-tail new activity
ledger stats ledger.db [--wf WORKFLOW]         # summary + duplicate-rate bar
ledger clear ledger.db [--wf WORKFLOW] [--yes] # wipe records
```

---

## Dashboard

```bash
ledger-dashboard ledger.db
```

Open `http://localhost:4242` to see every tool call in real time — what executed, what was blocked, which workflow triggered it, duplicate counts.

The dashboard is local — runs against your own `ledger.db`. No hosting, no auth, no setup.

---

## Multi-agent scoping

```python
guard.workflow(f"order-{order_id}")  # isolate records per order/request
guard.as_caller("agent-A")           # tag records with an identity
```

---

## Custom storage backend

Swap SQLite for Redis, Postgres, or DynamoDB by implementing six methods:

```python
from ledger import Store, Guard

class RedisStore(Store):
    def get(self, id): ...
    def claim(self, r) -> bool: ...
    def put(self, r): ...
    def delete(self, id): ...
    def all(self, wf=None): ...
    def clear(self, wf=None): ...

guard = Guard(store=RedisStore())
```

---

## Testing

```python
from ledger import Guard, _Mem

def test_no_duplicate_charge():
    guard = Guard(store=_Mem())   # fully isolated, no disk I/O
    guard(charge_card, card_id="tok_test", amount=49.00)
    guard(charge_card, card_id="tok_test", amount=49.00)  # blocked
    assert guard.stats()["executed"] == 1
    assert guard.stats()["blocked"]  == 1
```

---

## How it works internally

1. **Fingerprint** — `sha256(tool_name + normalized_args + workflow)[:32]`. Arg order and float precision are normalized so retries always match.
2. **Atomic claim** — SQLite `INSERT OR IGNORE` ensures only one process claims the call, even across concurrent workers.
3. **State machine** — `RUNNING → SUCCESS` or `RUNNING → FAILED`. Failed records auto-clear so broken tools are always retryable.
4. **Crash recovery** — Stale `RUNNING` records expire after a configurable timeout (default 300s) and allow fresh retries.

---

## Environment variables

| Variable          | Default       | Description                    |
| ----------------- | ------------- | ------------------------------ |
| `LEDGER_DB`       | `./ledger.db` | Database path                  |
| `LEDGER_WORKFLOW` | `"default"`   | Workflow scope                 |
| `LEDGER_QUIET`    | `"0"`         | Set to `"1"` to silence output |

```bash
LEDGER_DB=/data/agent.db LEDGER_WORKFLOW=run-42 python agent.py
```

---

## Concurrency

SQLite with WAL mode handles dozens of concurrent writers cleanly. For large clusters, swap in Redis or Postgres using the storage interface.

---

## Using Claude Code or Cursor?

Ledger ships with first-class support for AI coding assistants so you never have to explain the API from scratch.

### Copy-paste prompt — OpenAI / any dict-based framework

```
I want to add ledger-once to my agent to prevent duplicate tool calls on retry.

Step 1 — Find my tool_map dict (or wherever I dispatch tool calls by name).
Step 2 — Wrap it: tool_map = guard.wrap_tools(tool_map, blocked_return={"status": "blocked"})
Step 3 — Call tools directly in dispatch — no None check needed:
         result = tool_map[name](**args)
         return json.dumps(result)
Step 4 — For any read-only tools (search, fetch, lookup):
         guard.policy(fn, unlimited=True)
Step 5 — If any tool arguments include timestamps, UUIDs, or request IDs:
         add key=f"stable-{entity_id}" to that tool call.

Do not configure a database — Ledger auto-creates ledger.db.
Import only: from ledger import guard
```

### Copy-paste prompt — LangChain

```
I want to add ledger-once to my LangChain agent to prevent duplicate tool calls.

The rule for LangChain: wrap raw Python functions BEFORE StructuredTool.from_function().
Do NOT wrap StructuredTool objects directly.

Step 1 — Find my list of raw tool functions (before they become StructuredTools).
Step 2 — Wrap them: protected = guard.wrap_tools([fn1, fn2, fn3])
Step 3 — Rebuild my StructuredTools:
         tools = [StructuredTool.from_function(fn) for fn in protected]
Step 4 — For read-only tools: guard.policy(fn, unlimited=True)
Step 5 — If any tool arguments include timestamps or UUIDs:
         pass key=f"stable-{entity_id}" as an extra kwarg on that call.

Do not configure a database. Import only: from ledger import guard
```

### Copy-paste prompt — Async agent

```
I want to add ledger-once to my async agent. No await changes needed —
Ledger handles async automatically.

Step 1 — Wrap my tools: tool_map = guard.wrap_tools(tool_map, blocked_return={"status": "blocked"})
Step 2 — Call wrapped async tools normally:
         result = await tool_map["post_webhook"](url=url, payload=data)
Step 3 — For read-only tools: guard.policy(fn, unlimited=True)
Step 4 — If arguments include timestamps or UUIDs: add key=f"stable-{entity_id}"
```

### Verify it's working

```python
guard.log()
# ✓ send_email    attempts 3   executed 1   blocked 2   ← correct: ran once, blocked twice
# ✗ charge_card   attempts 1   executed 0               ← tool raised; auto-retryable
```

```bash
ledger stats ledger.db      # summary + duplicate-rate bar
ledger show  ledger.db      # per-tool history table
ledger-dashboard ledger.db  # full web UI at http://localhost:4242
```

### Files included for AI assistants

| File | Purpose | Where AI reads it |
|------|---------|-------------------|
| `llms.txt` | Machine-readable API surface with explicit code-generation rules | Auto-fetched by Claude Code, Cursor, and other assistants |
| `CLAUDE.md` | Project briefing for Claude Code | Read automatically when Claude Code enters your project directory |
| `examples/example_openai.py` | Full OpenAI function-calling loop with retry simulation | Discovered via semantic search |
| `examples/example_langchain.py` | LangChain StructuredTool pattern with real agent setup | Discovered via semantic search |

---

**Your agent retries. Your users never feel it.**
