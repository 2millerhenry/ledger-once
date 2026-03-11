# Ledger

**Exactly-once execution for AI agent tool calls.**

Your AI agent will retry. Ledger makes sure it doesn't matter.

One line prevents duplicate charges, emails, webhooks, and database writes.

```python
from ledger import guard

guard(stripe_charge, customer="cus_42", amount=99)   # 💳 charged — runs     ✓
guard(stripe_charge, customer="cus_42", amount=99)   # blocked  ✗
guard(stripe_charge, customer="cus_42", amount=99)   # blocked  ✗
```

![Ledger demo](Docs/LEDGERGIF-ezgif.com-video-to-gif-converter.gif)


---

## Why this exists

AI agents retry tools. That's how they're designed.

Timeouts, crashes, and loops mean the same tool call can execute multiple times. When that tool has side effects — charges, emails, webhooks — retries duplicate real actions.

Ledger intercepts every tool call, fingerprints it, and guarantees the tool executes **exactly once**.

It doesn't matter if it's:

* the same process retrying
* multiple workers racing
* or a crash and full restart

Records persist automatically to SQLite, so the guarantee survives restarts with zero configuration.

---

## Real failures this prevents

Duplicate Stripe charges. Duplicate refunds. Duplicate emails.
Duplicate webhooks. Duplicate database writes.

If your agent calls external APIs, you already have this risk.

---

## Install

```bash
pip install ledger-once
```

Or just copy the file — zero dependencies, no setup:

```bash
cp ledger.py your_project/
```

Python 3.10+. Uses only the standard library.

---

## Quickstart

### Protect a single call

```python
from ledger import guard

guard(stripe_charge, customer="cus_42", amount=99)
```

### Protect your entire toolset

```python
tools = guard.wrap_tools(tools)
```

Works with a list or dictionary. Every tool becomes automatically protected.

Every tool now runs **at most once per unique argument set**, even across restarts.

---

## Mental model

Ledger brings the idea of **idempotency keys** to AI agents.

Stripe uses idempotency keys to ensure a charge executes once.

Ledger applies the same guarantee to **tool calls inside agent systems**.

---

## Works with every framework

### OpenAI

```python
from ledger import guard

tool_map = guard.wrap_tools({
    "send_email":  send_email,
    "charge_card": stripe_charge,
})

def dispatch_tool(name: str, arguments: dict) -> str:
    fn = tool_map[name]
    result = fn(**arguments)
    return json.dumps(result if result is not None else {"status": "blocked"})
```

A runnable example is included in `examples/example_openai.py`.

---

### LangChain

```python
from ledger import guard
from langchain_core.tools import StructuredTool

# Wrap the raw Python functions first — not StructuredTool objects
protected = guard.wrap_tools([send_email, stripe_charge])
agent_tools = [StructuredTool.from_function(fn) for fn in protected]
```

A runnable example is included in `examples/example_langchain.py`.

---

### Any framework

```python
tools = guard.wrap_tools([
    search_web,
    send_email,
    stripe_charge,
    create_ticket,
])

agent = YourAgent(tools=tools)
```

---

## The `key=` pattern

If your arguments include timestamps, UUIDs, or random values, each call appears unique and Ledger cannot detect duplicates.

Use a stable key:

```python
# ✗ BAD — timestamp changes every call
guard(send_email, to="user@x.com", sent_at=datetime.now())

# ✓ GOOD — stable key across retries
guard(send_email, to="user@x.com", sent_at=datetime.now(), key=f"email-{order_id}")
```

Same idea as **Stripe idempotency keys**.

**Built-in footgun detector:** If Ledger sees the same tool called 3+ times with different fingerprints within 10 seconds and no `key=` was given, it prints a warning automatically:

```
[ledger] ⚠  send_email called 4× with different args in <10s — if retrying, add key=
           guard(send_email, ..., key="your-stable-id")
```

---

## Per-tool policies

```python
guard.policy(search_web,    unlimited=True)  # read-only: always run, never block
guard.policy(stripe_charge, replay=True)     # blocked callers get the cached result
guard.policy(send_sms,      max=2)           # allow up to 2 executions
guard.policy(daily_report,  ttl=86400)       # once per day — forget after 24h
```

---

## Async

Works automatically.

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
python ledger_dashboard.py
```

Open `http://localhost:4242` to see every tool call in real time:

* what executed
* what was blocked
* which workflow triggered it
* duplicate counts

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
guard.reset()  # clear all records between tests
```

Or use a fully isolated in-memory store with no file I/O:

```python
from ledger import Guard, _Mem

def test_no_duplicate_charge():
    guard = Guard(store=_Mem())
    guard(charge_card, card_id="tok_test", amount=49.00)
    guard(charge_card, card_id="tok_test", amount=49.00)  # blocked
    assert guard.stats()["executed"] == 1
    assert guard.stats()["blocked"]  == 1
```

---

## How it works

1. **Fingerprint** — `(tool_name, args, workflow)` is hashed into a stable ID. Argument order and float precision are normalized so retries always match.

2. **Atomic claim** — SQLite `INSERT OR IGNORE` ensures only one process claims the call, even across concurrent workers.

3. **State machine** — `RUNNING → SUCCESS` or `RUNNING → FAILED`. Failed records auto-clear so broken tools are always retryable.

4. **Crash recovery** — Stale `RUNNING` records expire after a configurable timeout (default 300s) and allow fresh retries.

---

## Environment variables

| Variable          | Default       | Description                     |
| ----------------- | ------------- | ------------------------------- |
| `LEDGER_DB`       | `./ledger.db` | Database path                   |
| `LEDGER_WORKFLOW` | `"default"`   | Workflow scope                  |
| `LEDGER_QUIET`    | `"0"`         | Set to `"1"` to silence output  |

```bash
LEDGER_DB=/data/agent.db LEDGER_WORKFLOW=run-42 python agent.py
```

---

## Concurrency

SQLite with WAL mode handles dozens of concurrent writers cleanly.

For large clusters, swap in Redis or Postgres using the storage interface.

---

## Using Claude Code or Cursor?

Ledger ships with first-class support for AI coding assistants so you never have to explain the API from scratch.

### Copy-paste prompt — OpenAI / any dict-based framework

```
I want to add ledger-once to my agent to prevent duplicate tool calls on retry.

Step 1 — Find my tool_map dict (or wherever I dispatch tool calls by name).
Step 2 — Wrap it: tool_map = guard.wrap_tools(tool_map)
Step 3 — In my dispatch function, handle the blocked case:
         result = tool_map[name](**args)
         return json.dumps(result if result is not None else {"status": "blocked"})
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

Step 1 — Wrap my tools: tool_map = guard.wrap_tools(tool_map)
Step 2 — Call wrapped async tools normally:
         result = await tool_map["post_webhook"](url=url, payload=data)
Step 3 — Handle blocked returns: result is None when blocked.
Step 4 — For read-only tools: guard.policy(fn, unlimited=True)
Step 5 — If arguments include timestamps or UUIDs: add key=f"stable-{entity_id}"
```

### Verify it's working

After adding Ledger, trigger a retry in your agent and check:

```python
guard.log()
# ✓ send_email    attempts 3   executed 1   blocked 2   ← correct: ran once, blocked twice
# ✗ charge_card   attempts 1   executed 0               ← tool raised; auto-retryable

guard.stats()
# {'actions': 2, 'attempts': 4, 'executed': 1, 'blocked': 2, 'failed': 1}
```

Or inspect `ledger.db` directly from the terminal:

```bash
ledger stats ledger.db   # summary + duplicate-rate bar
ledger show  ledger.db   # per-tool history table
ledger-dashboard ledger.db  # full web UI at http://localhost:4242
```

A correct integration shows `executed: 1` for side-effecting tools even when `attempts` is 2 or more.

### Files included for AI assistants

These files are in the repo specifically so AI coding assistants can understand Ledger without hallucinating the API:

| File | Purpose | Where AI reads it |
|------|---------|-------------------|
| `llms.txt` | Machine-readable API surface with explicit code-generation rules | Auto-fetched by Claude Code, Cursor, and other assistants when added to your project |
| `CLAUDE.md` | Project briefing for Claude Code | Read automatically when Claude Code enters your project directory |
| `examples/example_openai.py` | Full OpenAI function-calling loop with retry simulation | Discovered via semantic search when AI asks "how do I use Ledger with OpenAI?" |
| `examples/example_langchain.py` | LangChain StructuredTool pattern with real agent setup | Discovered via semantic search when AI asks "how do I use Ledger with LangChain?" |

**`CLAUDE.md`** lives at the repo root. Claude Code reads it automatically on startup and uses it as a project briefing — it contains the complete API surface, common mistakes, and rules that prevent the most common hallucinations (like assuming a blocked call raises an exception, or wrapping LangChain tools incorrectly).

**`llms.txt`** follows the [llms.txt standard](https://llmstxt.org) — a markdown file at the repo root written specifically for LLMs, stripped of marketing, containing only the raw API and rules. Any assistant that fetches it gets zero-shot accuracy on Ledger's syntax.

**`examples/`** is the fallback. When an AI coding assistant uses semantic search (ripgrep) across installed packages to understand how something works, it finds these files first. Both examples are self-contained and runnable without an API key — they include a simulated retry so you can see Ledger working before connecting to a real service.

---

**Your agent retries. Your users never feel it.**
