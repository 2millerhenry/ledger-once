"""
example_langchain.py — Ledger + LangChain, end-to-end.

Demonstrates exactly-once tool execution in a LangChain agent that retries
after a simulated timeout. Ledger blocks the duplicate charge and email
while allowing the read-only search to run again.

── Quick start ────────────────────────────────────────────────────────────────

    # Simulated run (no API key needed):
    python example_langchain.py

    # Real agent (requires API key):
    pip install langchain-core langchain-openai
    export OPENAI_API_KEY=sk-...
    python example_langchain.py --live

── The one rule for LangChain ────────────────────────────────────────────────

    Wrap the RAW Python functions — not StructuredTool objects.

    # WRONG — wraps the StructuredTool wrapper, not the underlying function
    guard.wrap_tools([langchain_tool])

    # CORRECT — wrap first, then build StructuredTool
    protected = guard.wrap_tools([send_email, charge_card])
    tools = [StructuredTool.from_function(fn) for fn in protected]

── Tested with ────────────────────────────────────────────────────────────────
    langchain-core==0.2.0
    langchain-openai==0.1.0
    openai==1.30.0
"""

from __future__ import annotations

import argparse
import json

from ledger import Guard, _Mem, guard as default_guard


# ─── 1. Define your tools ─────────────────────────────────────────────────────
#
# Normal Python functions. No Ledger imports here.
# Keep tools focused: one side effect each.

def send_email(to: str, subject: str, body: str) -> dict:
    """Send a transactional email.

    Example:
        result = send_email(
            to="user@example.com",
            subject="Your receipt",
            body="You were charged $49.",
        )

    Returns:
        {"status": "sent", "to": str}
    """
    print(f"    📧 send_email → {to!r}")
    return {"status": "sent", "to": to}


def charge_card(card_id: str, amount: float) -> dict:
    """Charge a customer's payment card.

    Example:
        result = charge_card(card_id="tok_visa", amount=49.00)

    Returns:
        {"status": "charged", "charge_id": str, "amount": float}

    Raises:
        ValueError: if card_id is empty or amount <= 0
    """
    if not card_id or amount <= 0:
        raise ValueError(f"Invalid charge: card_id={card_id!r}, amount={amount}")
    print(f"    💳 charge_card → {card_id!r} ${amount}")
    return {"status": "charged", "charge_id": "ch_0001", "amount": amount}


def search_web(query: str) -> dict:
    """Search the web for information. Read-only — safe to run on every retry.

    Example:
        result = search_web(query="Stripe refund policy 2024")

    Returns:
        {"results": list[str]}
    """
    print(f"    🔍 search_web → {query!r}")
    return {"results": [f"Top result for: {query}"]}


# ─── 2. Protect tools with Ledger ────────────────────────────────────────────
#
# Use Guard(store=_Mem()) here so the example runs without touching disk.
# In production, use the default: guard = Guard()  (auto SQLite)

_guard = Guard(store=_Mem())
_guard.quiet()  # silence per-call output for this demo

# Mark search_web as unlimited — it's a read, not a write
_guard.policy(search_web, unlimited=True)

# Wrap raw functions. Blocked calls return None (not an exception).
_protected = _guard.wrap_tools([send_email, charge_card, search_web])

# Build a name-keyed lookup for dispatch
_tool_map: dict[str, object] = {fn.__name__: fn for fn in _protected}


# ─── 3. Build LangChain StructuredTools ──────────────────────────────────────
#
# Uncomment this block when langchain-core is installed.
# This is the only change from a normal LangChain setup.

def build_langchain_tools():
    """Return list[StructuredTool] with Ledger protection baked in."""
    from langchain_core.tools import StructuredTool  # type: ignore
    return [StructuredTool.from_function(fn) for fn in _protected]


# ─── 4. Real agent with ChatOpenAI (--live mode) ─────────────────────────────

def run_live_agent() -> None:
    """Run a real LangChain agent against OpenAI. Requires OPENAI_API_KEY."""
    from langchain_openai import ChatOpenAI             # type: ignore
    from langchain.agents import AgentExecutor, create_tool_calling_agent  # type: ignore
    from langchain_core.prompts import ChatPromptTemplate  # type: ignore

    tools = build_langchain_tools()
    llm   = ChatOpenAI(model="gpt-4o", temperature=0)

    prompt = ChatPromptTemplate.from_messages([
        ("system", "You are a helpful billing assistant. Use the tools provided."),
        ("human",  "{input}"),
        ("placeholder", "{agent_scratchpad}"),
    ])

    agent    = create_tool_calling_agent(llm, tools, prompt)
    executor = AgentExecutor(agent=agent, tools=tools, verbose=True)

    print("\n  Running live agent...\n")
    result = executor.invoke({
        "input": "Charge card tok_visa $49 and send a receipt to user@example.com. "
                 "Also search for the current Stripe refund policy."
    })
    print(f"\n  Agent output: {result['output']}")

    _guard.log()
    print(f"\n  Stats: {_guard.stats()}")


# ─── 5. Simulated agent loop (no API key needed) ─────────────────────────────

def run_simulated_agent() -> None:
    """Simulate a LangChain agent loop that retries after a fake timeout.

    Turn 1: charge card, send email, search web — all execute normally.
    Turn 2: agent retries (simulated timeout) — charge and email are blocked,
            search runs again because it has unlimited=True policy.
    """
    print("\n  Simulating LangChain agent loop with retry...\n")

    # Simulate two turns of tool calls (what the agent decides to do)
    turns = [
        {
            "label": "Turn 1 — initial run",
            "calls": [
                ("charge_card", {"card_id": "tok_visa",           "amount": 49.00}),
                ("send_email",  {"to": "user@example.com",
                                 "subject": "Your receipt",
                                 "body": "You were charged $49."}),
                ("search_web",  {"query": "Stripe refund policy"}),
            ],
        },
        {
            "label": "Turn 2 — agent retries after simulated timeout",
            "calls": [
                ("charge_card", {"card_id": "tok_visa",           "amount": 49.00}),
                ("send_email",  {"to": "user@example.com",
                                 "subject": "Your receipt",
                                 "body": "You were charged $49."}),
                ("search_web",  {"query": "Stripe refund policy"}),  # unlimited → runs again
            ],
        },
    ]

    for turn in turns:
        print(f"  ── {turn['label']} ──────────────────────────────")
        for name, kwargs in turn["calls"]:
            fn     = _tool_map[name]
            before = _guard.stats()["executed"]
            result = fn(**kwargs)
            after  = _guard.stats()["executed"]

            executed = after > before
            status   = "✓ executed" if executed else "✗ blocked  (Ledger prevented duplicate)"
            print(f"    {name:<20}  {status}")

            # This is what your real dispatch would return to the agent:
            _ = json.dumps(result if result is not None else {"status": "blocked"})
        print()

    stats = _guard.stats()
    print("  ── Results ─────────────────────────────────────────")
    print(f"  attempts  : {stats['attempts']}")
    print(f"  executed  : {stats['executed']}")
    print(f"  blocked   : {stats['blocked']}")
    print()

    charge_ran  = sum(1 for r in _guard.history(tool=charge_card) if r["runs"] > 0)
    email_sent  = sum(1 for r in _guard.history(tool=send_email)  if r["runs"] > 0)
    print(f"  charge_card ran  : {charge_ran}×  (should be 1 — no duplicate charge)")
    print(f"  send_email ran   : {email_sent}×  (should be 1 — no duplicate email)")
    print(f"  search_web ran   : 2×  (expected — unlimited policy allows retries)")

    ok = charge_ran == 1 and email_sent == 1
    print(f"\n  {'✅ Correct — Ledger prevented all duplicates' if ok else '❌ Something ran more than once'}\n")


# ─── Entry point ──────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Ledger + LangChain example")
    parser.add_argument("--live", action="store_true",
                        help="Run real agent (requires OPENAI_API_KEY and langchain-openai)")
    args = parser.parse_args()

    print("\n" + "═" * 55)
    print("  Ledger + LangChain — exactly-once tool execution")
    print("  Tested with langchain-core==0.2.0, openai==1.30.0")
    print("═" * 55)

    if args.live:
        run_live_agent()
    else:
        run_simulated_agent()
        print("  To run against a real OpenAI agent:")
        print("    pip install langchain-core langchain-openai")
        print("    export OPENAI_API_KEY=sk-...")
        print("    python example_langchain.py --live\n")


if __name__ == "__main__":
    main()
