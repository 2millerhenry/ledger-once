# Ledger — AI Integration Brief

**Ledger Once** gives any AI agent tool call exactly-once execution semantics.
It prevents duplicate charges, emails, webhooks, and database writes when agents retry.
Think Stripe idempotency keys, but automatic and applied to every tool call.

---

## Mental model (read this first)

Ledger fingerprints every call by `(tool_name, normalized_args, workflow)` and stores
the state in SQLite. On retry, it sees the same fingerprint and blocks the call.

**Critical behaviors to understand before generating code:**
- A **blocked call returns `None`** (not an error). Your dispatch loop must handle this.
- **`FAILED` records auto-clear** — tools that raise exceptions are always retryable.
- **`RUNNING` records expire** after `timeout` seconds (default: 300s) for crash recovery.
- Argument order doesn't matter: `guard(fn, x=1, y=2) == guard(fn, y=2, x=1)`
- Positional == keyword: `guard(fn, 42) == guard(fn, x=42)`
- Float precision is normalized: `99.99 == 99.9900000001`

---

## Imports

```python
from ledger import guard           # global singleton — SQLite-backed, zero config
from ledger import Guard, _Mem     # for custom instances or isolated tests
from ledger import Store           # protocol for custom storage backends (Redis, etc.)
```

Do NOT import `_SQLite` directly in application code — it is internal.

---

## The four usage patterns

### 1. Wrap an entire toolset (use this for agent frameworks)

```python
from ledger import guard

tool_map = guard.wrap_tools({
    "send_email":    send_email,
    "charge_card":   stripe_charge,
    "create_ticket": create_ticket,
})

# tool_map is a plain dict — dispatch exactly as you would without Ledger.
# A blocked call returns None — handle it explicitly in your dispatch function:
def dispatch(name: str, args: dict) -> str:
    result = tool_map[name](**args)
    return json.dumps(result if result is not None else {"status": "blocked"})
```

### 2. Protect a single call inline

```python
result = guard(stripe_charge, customer="cus_42", amount=99)
# Returns the tool's return value if executed, None if blocked.
```

### 3. Decorator at definition time

```python
@guard.once
def stripe_charge(card_id, amount): ...

@guard.once(replay=True)   # blocked callers receive the cached first-run result
def create_invoice(id, amount): ...
```

### 4. List wrapping — for LangChain and list-based frameworks

```python
# Always wrap the raw Python functions BEFORE passing to framework wrappers.
# WRONG:  guard.wrap_tools([langchain_tool_object])  ← wraps StructuredTool, not the fn
# CORRECT:
protected = guard.wrap_tools([send_email, stripe_charge, search_web])

from langchain_core.tools import StructuredTool
agent_tools = [StructuredTool.from_function(fn) for fn in protected]
```

---

## The `key=` pattern — required for non-deterministic arguments

If any argument contains a **timestamp, UUID, request ID, or random value**, the
fingerprint changes every retry and Ledger cannot detect duplicates. Always pass `key=`.

```python
# WRONG — each retry has a new timestamp → new fingerprint → executes again
guard(send_email, to="user@x.com", sent_at=datetime.now())

# CORRECT — stable key ties all retries to the same fingerprint
guard(send_email, to="user@x.com", sent_at=datetime.now(), key=f"email-{order_id}")

# With wrap_tools — pass key= as a regular kwarg to the wrapped function:
tool_map["send_email"](to="user@x.com", sent_at=datetime.now(), key=f"email-{order_id}")
```

**Built-in footgun detector:** If the same tool is called 3+ times with different
fingerprints within 10 seconds and no `key=` was given, Ledger prints:
```
[ledger] ⚠  send_email called 4× with different args in <10s — if retrying, add key=
           guard(send_email, ..., key="your-stable-id")
```

---

## Per-tool policies — always set before the agent loop starts

```python
guard.policy(search_web,    unlimited=True)  # read-only: always run, never block
guard.policy(stripe_charge, replay=True)     # blocked callers receive cached result
guard.policy(send_sms,      max=2)           # allow at most 2 executions, then block
guard.policy(daily_report,  ttl=86400)       # forget after 24h; allow once per day
```

Policy params: `unlimited`, `replay`, `max`, `ttl`, `timeout` (crash recovery window, default 300s).
Policies set after tool calls have already run do NOT retroactively affect existing records.

---

## Configuration

```python
guard.persist("/data/myagent.db")    # override DB path in code; returns guard for chaining
guard.workflow(f"order-{order_id}")  # scope deduplication — MUST be called before tool calls
guard.as_caller("agent-A")           # tag all records with an identity string
guard.quiet()                        # silence console output; guard.log() still works
guard.on_block(lambda r: metrics.increment("ledger.blocked", tags={"tool": r.tool}))
```

**Environment variables (preferred for production):**

| Variable        | Default       | Description                     |
|-----------------|---------------|---------------------------------|
| LEDGER_DB       | ./ledger.db   | SQLite file path                |
| LEDGER_WORKFLOW | "default"     | Workflow scope                  |
| LEDGER_QUIET    | "0"           | Set "1" to silence all output   |

```bash
LEDGER_DB=/data/agent.db LEDGER_WORKFLOW=run-42 python agent.py
```

---

## Async — zero changes needed

```python
result = await guard(post_webhook, url="...", payload=data)
# wrap_tools() also detects and handles async functions automatically.
```

---

## Observability

```python
guard.log()
# Prints:
# ✓ send_email    attempts 3   executed 1   blocked 2   <- retried 2x
# ✗ charge_card   attempts 1   executed 0               -> CardError: declined

guard.stats()
# Returns: {'actions': 2, 'attempts': 4, 'executed': 1, 'blocked': 2, 'failed': 1}

guard.history()                  # list[dict] of all records as serializable dicts
guard.history(tool=send_email)   # filter by tool function reference
guard.history(wf="order-42")     # filter by workflow id
```

---

## Escape hatches

```python
guard.retry(send_email, to="user@example.com")  # clear record → next call executes fresh
guard.force(send_email, to="user@example.com")  # execute NOW regardless of history
```

---

## Testing

```python
from ledger import Guard, _Mem

# Preferred: fresh in-memory store per test — no file I/O, fully isolated
def test_my_agent():
    guard = Guard(store=_Mem())
    # ... run agent ...
    assert guard.stats()["executed"] == 1
    assert guard.stats()["blocked"] == 2

# Alternative: reset the global singleton between tests
def teardown():
    guard.reset()              # clear all records
    guard.reset(wf="order-1")  # clear only one workflow
```

---

## Custom storage backend

```python
from ledger import Store, Guard, Record

class RedisStore(Store):
    def get(self, id: str) -> Record | None: ...       # fetch by fingerprint id
    def claim(self, r: Record) -> bool: ...            # atomic insert-if-absent → True if won
    def put(self, r: Record) -> None: ...              # upsert full record
    def delete(self, id: str) -> None: ...             # remove by id
    def all(self, wf: str | None = None) -> list: ...  # list all, optionally filtered by wf
    def clear(self, wf: str | None = None) -> None: ...

guard = Guard(store=RedisStore())
```

---

## Multi-agent scoping

```python
# Isolate records per order/request — different runs never block each other:
guard.workflow(f"order-{order_id}")

# Tag every record written by this guard instance:
guard.as_caller(f"agent-{agent_id}")

# Then inspect by scope:
guard.log(wf=f"order-{order_id}")
guard.history(wf=f"order-{order_id}")
```

---

## CLI

```bash
ledger show  ledger.db [--wf WORKFLOW]         # full history table
ledger tail  ledger.db [--wf WORKFLOW]         # live-tail new activity (Ctrl-C to stop)
ledger stats ledger.db [--wf WORKFLOW]         # summary numbers + duplicate-rate bar
ledger clear ledger.db [--wf WORKFLOW] [--yes] # wipe records
ledger-dashboard ledger.db                     # web UI → http://localhost:4242
```

---

## What NOT to protect

- **Read-only tools** (search, fetch, lookup) → `guard.policy(fn, unlimited=True)`
- **Already-idempotent tools** where duplicates are harmless by design
- **Tools inside a tight retry loop you fully own** with no cross-process concerns

---

## Common mistakes

| Mistake | Correct behavior / fix |
|---------|------------------------|
| Expecting a blocked call to raise an exception | It returns `None` silently. Check `if result is not None` in dispatch. |
| Calling `guard.workflow()` after tools have run | Must be called before any tool calls in the session. |
| Passing `datetime.now()` or `uuid4()` as args without `key=` | Add `key=f"stable-{entity_id}"` |
| Wrapping `StructuredTool` objects in LangChain | Wrap `.func` first, then `StructuredTool.from_function(fn)` |
| Using `Guard(store=_Mem())` in production | `_Mem` is lost on restart. Use SQLite (default) or a persistent backend. |
| Calling `guard.policy()` after the agent loop starts | Policies only apply to calls made after they are registered. |
| Manually retrying after a tool raises | Don't — `FAILED` records auto-clear. The next call will execute automatically. |
| Chaining `.persist().workflow().as_caller()` in wrong order | Order doesn't matter — all three return `guard` for chaining. |

---

## File layout

```
ledger.py              — entire library, zero dependencies, copy-paste friendly
ledger_cli.py          — CLI (show / tail / stats / clear)
ledger_dashboard.py    — web dashboard (http://localhost:4242)
examples/
  example_openai.py    — complete OpenAI function-calling agent loop with retry simulation
  example_langchain.py — LangChain StructuredTool pattern with real agent setup
```
