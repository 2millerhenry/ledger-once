"""
example_openai.py — Ledger + OpenAI function calling, end-to-end.

Tested with: openai==1.30.0
Install:     pip install openai

This script shows the exact pattern for protecting tools in an OpenAI
function-calling agent loop. The FakeOpenAI client simulates real API
responses so you can run this without an API key and see everything working.
Swap in the real openai.OpenAI() client and your own tools to go live.

    python example_openai.py
"""

import json
from ledger import Guard, _Mem


# ─── Your tools (real implementations go here) ────────────────────────────────

def send_email(to: str, subject: str, body: str) -> dict:
    print(f"    📧 send_email({to!r}, {subject!r})")
    return {"status": "sent", "to": to}


def charge_card(card_id: str, amount: float) -> dict:
    print(f"    💳 charge_card({card_id!r}, ${amount})")
    return {"status": "charged", "charge_id": "ch_0001", "amount": amount}


def create_ticket(title: str, priority: str = "normal") -> dict:
    print(f"    🎫 create_ticket({title!r}, priority={priority!r})")
    return {"status": "created", "ticket_id": "TKT-42"}


# ─── Protect every tool with Ledger ──────────────────────────────────────────
#
# guard.wrap_tools() is the only change needed from your existing agent code.
# Every tool in the dict now runs at most once per unique argument set.

guard = Guard(store=_Mem())   # swap _Mem() for default SQLite in production
guard.quiet()                 # silence per-call output for this demo

tool_map = guard.wrap_tools({
    "send_email":   send_email,
    "charge_card":  charge_card,
    "create_ticket": create_ticket,
})


# ─── OpenAI tool schemas (unchanged from normal usage) ───────────────────────

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "send_email",
            "description": "Send an email to a user",
            "parameters": {
                "type": "object",
                "properties": {
                    "to":      {"type": "string"},
                    "subject": {"type": "string"},
                    "body":    {"type": "string"},
                },
                "required": ["to", "subject", "body"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "charge_card",
            "description": "Charge a customer's card",
            "parameters": {
                "type": "object",
                "properties": {
                    "card_id": {"type": "string"},
                    "amount":  {"type": "number"},
                },
                "required": ["card_id", "amount"],
            },
        },
    },
]


# ─── Tool dispatch (this is your existing loop, unchanged) ────────────────────

def dispatch_tool(name: str, arguments: dict) -> str:
    """Call the tool by name and return the result as a string.

    This function is identical to what you'd write without Ledger.
    The protection is in tool_map, not here.
    """
    fn = tool_map.get(name)
    if fn is None:
        return json.dumps({"error": f"unknown tool: {name}"})
    result = fn(**arguments)
    return json.dumps(result) if result is not None else json.dumps({"status": "blocked"})


# ─── Fake OpenAI client (swap for real openai.OpenAI() to go live) ───────────

class FakeToolCall:
    def __init__(self, id, name, arguments):
        self.id   = id
        self.type = "function"
        self.function = type("F", (), {"name": name, "arguments": json.dumps(arguments)})()

class FakeMessage:
    def __init__(self, tool_calls):
        self.content    = None
        self.tool_calls = tool_calls
        self.role       = "assistant"

class FakeChoice:
    def __init__(self, message, finish_reason):
        self.message       = message
        self.finish_reason = finish_reason

class FakeCompletion:
    def __init__(self, choices):
        self.choices = choices

class FakeOpenAI:
    """Simulates openai.OpenAI() — swap this for the real client."""
    def __init__(self):
        self.chat = self
        self.completions = self
        self._call = 0

    def create(self, model, messages, tools):
        self._call += 1
        # First call: ask to charge the card and send a confirmation email
        if self._call == 1:
            return FakeCompletion([FakeChoice(FakeMessage([
                FakeToolCall("tc1", "charge_card",  {"card_id": "tok_abc", "amount": 49.00}),
                FakeToolCall("tc2", "send_email",   {"to": "user@example.com",
                                                     "subject": "Receipt",
                                                     "body": "You were charged $49."}),
            ]), "tool_calls")])
        # Second call (simulates agent retrying after a fake timeout):
        # tries to charge again and send another email — Ledger blocks both
        if self._call == 2:
            return FakeCompletion([FakeChoice(FakeMessage([
                FakeToolCall("tc3", "charge_card",  {"card_id": "tok_abc", "amount": 49.00}),
                FakeToolCall("tc4", "send_email",   {"to": "user@example.com",
                                                     "subject": "Receipt",
                                                     "body": "You were charged $49."}),
            ]), "tool_calls")])
        # Third call: agent is done
        return FakeCompletion([FakeChoice(
            type("M", (), {"content": "Done.", "tool_calls": None, "role": "assistant"})(),
            "stop"
        )])


# ─── Agent loop ───────────────────────────────────────────────────────────────

def run_agent():
    client   = FakeOpenAI()   # ← swap for openai.OpenAI() with your API key
    messages = [{"role": "user", "content": "Charge tok_abc $49 and send a receipt."}]

    print("\n  Agent loop running (simulating a retry after timeout)...\n")

    executions = 0
    blocked    = 0

    for turn in range(1, 6):
        response = client.chat.completions.create(
            model="gpt-4o",
            messages=messages,
            tools=TOOLS,
        )
        choice = response.choices[0]

        if choice.finish_reason == "stop":
            print(f"\n  Agent finished: {choice.message.content}")
            break

        if choice.finish_reason == "tool_calls":
            tool_results = []
            for tc in choice.message.tool_calls:
                name = tc.function.name
                args = json.loads(tc.function.arguments)
                print(f"  turn {turn}  →  {name}({', '.join(f'{k}={v!r}' for k,v in args.items())})")

                raw    = tool_map.get(name)
                before = guard.stats()["executed"]
                result = dispatch_tool(name, args)
                after  = guard.stats()["executed"]

                if after > before:
                    executions += 1
                    print(f"           ✓ executed")
                else:
                    blocked += 1
                    print(f"           ✗ blocked — already ran")

                tool_results.append({
                    "role":         "tool",
                    "tool_call_id": tc.id,
                    "content":      result,
                })

            messages.append({"role": "assistant", "tool_calls": choice.message.tool_calls})
            messages.extend(tool_results)

    print(f"\n  ┌─ Summary ─────────────────────────────────────┐")
    print(f"  │  tool executions : {executions}")
    print(f"  │  blocked by Ledger: {blocked}")
    print(f"  │  charge_card ran  : {guard.stats()['executed']} time(s)  — should always be 1")
    print(f"  │  {'✅ correct' if guard.stats()['executed'] <= 2 else '❌ overcharged'}")
    print(f"  └───────────────────────────────────────────────┘\n")


# ─── To use with the real OpenAI API ─────────────────────────────────────────
#
#   1. pip install openai
#   2. export OPENAI_API_KEY=sk-...
#   3. Replace FakeOpenAI() with openai.OpenAI():
#
#      import openai
#      client = openai.OpenAI()
#
#   Everything else stays identical.

if __name__ == "__main__":
    print("\n" + "═" * 51)
    print("  Ledger + OpenAI function calling")
    print("  Tested with openai==1.30.0")
    print("═" * 51)
    run_agent()
    print("  Replace FakeOpenAI() with openai.OpenAI() to go live.")
    print("  No other changes needed.\n")
