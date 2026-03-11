"""
example.py — See Ledger working in 60 seconds.

    python example.py

Three scenarios. Each shows the problem first, then the fix.
No external dependencies — just ledger.py in the same directory.
"""

import threading
from ledger import Guard, _Mem

# Every scenario uses an isolated in-memory store — no ledger.db written,
# no cleanup needed, fully reproducible every run.
def fresh_guard():
    return Guard(store=_Mem())


# ─────────────────────────────────────────────────────────────────────────────
#  Shared: simulated Stripe API
#  Tracks how many times it was actually called so we can assert correctness.
# ─────────────────────────────────────────────────────────────────────────────

class FakeStripe:
    def __init__(self):
        self.call_count = 0
        self.total_charged = 0.0

    def charge(self, customer_id: str, amount: float) -> dict:
        self.call_count += 1
        self.total_charged += amount
        return {"charge_id": f"ch_{self.call_count:04d}", "amount": amount}


# ─────────────────────────────────────────────────────────────────────────────
#  SCENARIO 1 — Agent retry loop
#
#  An LLM agent retries a tool call when it doesn't get a response fast enough.
#  The charge already went through on the first attempt. Every retry double-bills.
# ─────────────────────────────────────────────────────────────────────────────

def scenario_retry_loop():
    _header("SCENARIO 1 — Agent retry loop")
    print("  The agent calls stripe_charge, gets no confirmation, retries 4 more times.")
    print("  The charge went through on attempt 1. Every retry is a duplicate.\n")

    stripe = FakeStripe()

    # --- Without Ledger ---
    print("  ┌─ WITHOUT Ledger ──────────────────────────────────────┐")
    for attempt in range(1, 6):
        result = stripe.charge("cus_42", 49.00)
        print(f"  │  attempt {attempt} → charged ${result['amount']}  [{result['charge_id']}]")
    print(f"  │")
    print(f"  │  stripe.charge called {stripe.call_count}×   total billed: ${stripe.total_charged:.2f}")
    print(f"  │  ❌ customer overcharged by ${stripe.total_charged - 49:.2f}")
    print("  └───────────────────────────────────────────────────────┘\n")

    # --- With Ledger ---
    stripe2 = FakeStripe()
    guard = fresh_guard()
    guard.quiet()

    print("  ┌─ WITH Ledger ─────────────────────────────────────────┐")
    for attempt in range(1, 6):
        result = guard(stripe2.charge, "cus_42", 49.00)
        icon = "✓" if attempt == 1 else "✗"
        label = "executed" if attempt == 1 else "blocked — already ran"
        print(f"  │  attempt {attempt} → {icon} {label}")
    print(f"  │")
    print(f"  │  stripe.charge called {stripe2.call_count}×   total billed: ${stripe2.total_charged:.2f}")
    print(f"  │  ✅ exactly once, no matter how many retries")
    print("  └───────────────────────────────────────────────────────┘")

    print("\n  Change required in your code:")
    print("    before:  stripe_charge(customer_id, amount)")
    print("    after:   guard(stripe_charge, customer_id, amount)\n")


# ─────────────────────────────────────────────────────────────────────────────
#  SCENARIO 2 — Concurrent workers racing
#
#  Three parallel agent workers all believe they should process order-9981.
#  Without protection, all three fire the charge simultaneously.
# ─────────────────────────────────────────────────────────────────────────────

def scenario_concurrent_workers():
    _header("SCENARIO 2 — Concurrent workers racing")
    print("  Three parallel agent workers. Each believes it should process order-9981.")
    print("  All three fire at the same time.\n")

    stripe = FakeStripe()
    lock = threading.Lock()

    # --- Without Ledger ---
    print("  ┌─ WITHOUT Ledger ──────────────────────────────────────┐")

    def worker_unprotected(worker_id):
        result = stripe.charge("cus_42", 99.00)
        with lock:
            print(f"  │  worker {worker_id} → charged ${result['amount']}  [{result['charge_id']}]")

    threads = [threading.Thread(target=worker_unprotected, args=(i+1,)) for i in range(3)]
    for t in threads: t.start()
    for t in threads: t.join()

    print(f"  │")
    print(f"  │  stripe.charge called {stripe.call_count}×   total billed: ${stripe.total_charged:.2f}")
    print(f"  │  ❌ customer charged {stripe.call_count}× instead of once")
    print("  └───────────────────────────────────────────────────────┘\n")

    # --- With Ledger ---
    stripe2 = FakeStripe()
    guard = fresh_guard()
    guard.quiet()
    guard.workflow("order-9981")

    print("  ┌─ WITH Ledger ─────────────────────────────────────────┐")

    def worker_protected(worker_id):
        result = guard(stripe2.charge, "cus_42", 99.00)
        with lock:
            ran = result is not None
            icon = "✓" if ran else "✗"
            label = f"executed → ${result['amount']}" if ran else "blocked — another worker already ran this"
            print(f"  │  worker {worker_id} → {icon} {label}")

    threads = [threading.Thread(target=worker_protected, args=(i+1,)) for i in range(3)]
    for t in threads: t.start()
    for t in threads: t.join()

    print(f"  │")
    print(f"  │  stripe.charge called {stripe2.call_count}×   total billed: ${stripe2.total_charged:.2f}")
    print(f"  │  ✅ atomic claim — exactly one worker wins, others blocked")
    print("  └───────────────────────────────────────────────────────┘\n")


# ─────────────────────────────────────────────────────────────────────────────
#  SCENARIO 3 — Non-deterministic args and the key= fix
#
#  The agent passes a timestamp into the tool. Every call has a different
#  fingerprint so Ledger can't detect duplicates — until you add key=.
# ─────────────────────────────────────────────────────────────────────────────

def scenario_nondeterministic_args():
    _header("SCENARIO 3 — Non-deterministic args and the key= fix")
    print("  The agent passes a timestamp into send_email on every retry.")
    print("  Without key=, each call has a different fingerprint — Ledger can't detect")
    print("  duplicates and every retry sends a real email.\n")

    call_count = [0]

    def send_email(to: str, subject: str, sent_at: float) -> dict:
        call_count[0] += 1
        return {"status": "sent", "to": to, "call": call_count[0]}

    # --- Without key= ---
    guard = fresh_guard()
    guard.quiet()

    print("  ┌─ WITHOUT key= ────────────────────────────────────────┐")
    for i in range(1, 4):
        ts = 1_000_000.0 + i          # different each retry, as in real agent code
        result = guard(send_email, to="user@example.com", subject="Welcome", sent_at=ts)
        print(f"  │  retry {i} → ✓ executed  (ts={ts:.0f})  call #{result['call']}")
    print(f"  │")
    print(f"  │  send_email executed {call_count[0]}× — every retry fired")
    print(f"  │  ❌ 3 duplicate emails sent")
    print("  └───────────────────────────────────────────────────────┘\n")

    # --- With key= ---
    call_count[0] = 0
    guard2 = fresh_guard()
    guard2.quiet()
    order_id = "order-8812"

    print("  ┌─ WITH key= ───────────────────────────────────────────┐")
    for i in range(1, 4):
        ts = 1_000_000.0 + i
        result = guard2(send_email, to="user@example.com", subject="Welcome",
                        sent_at=ts, key=f"welcome-{order_id}")
        icon  = "✓" if i == 1 else "✗"
        label = f"executed  call #{result['call']}" if i == 1 else "blocked — key already used"
        print(f"  │  retry {i} → {icon} {label}")
    print(f"  │")
    print(f"  │  send_email executed {call_count[0]}× — key= pins the fingerprint")
    print(f"  │  ✅ 1 email sent regardless of timestamp changes")
    print("  └───────────────────────────────────────────────────────┘")

    print("\n  Rule: if your args change between retries, add key= based on")
    print("  your business identity (order id, user id, request id).\n")


# ─────────────────────────────────────────────────────────────────────────────
#  Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _header(title: str) -> None:
    print("\n" + "═" * 57)
    print(f"  {title}")
    print("═" * 57 + "\n")


# ─────────────────────────────────────────────────────────────────────────────
#  Run all three
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("\n  Ledger — three demos, one command.")
    print("  Each shows the problem first, then the fix.\n")

    scenario_retry_loop()
    scenario_concurrent_workers()
    scenario_nondeterministic_args()

    print("═" * 57)
    print("  Next steps:")
    print("  1. guard(your_tool, ...)              protect a single call")
    print("  2. guard.wrap_tools(tools)            protect a whole toolset")
    print("  3. guard(fn, ..., key='your-id')      fix non-deterministic args")
    print("  4. python -m ledger_cli show ledger.db  inspect what ran")
    print("═" * 57 + "\n")
