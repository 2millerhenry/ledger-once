"""
test_ledger.py — full test suite for ledger.py

    python -m unittest test_ledger           # run all
    python -m unittest test_ledger -v        # verbose
    python -m unittest test_ledger.TestBlock # single class

No pytest, no extra dependencies — stdlib unittest only.
"""

import asyncio
import threading
import time
import unittest

from ledger import Guard, _Mem, _SQLite, Status, Store, Record


# ─── Helpers ──────────────────────────────────────────────────────────────────

def fresh() -> Guard:
    """In-memory guard — no disk, no cleanup needed."""
    return Guard(store=_Mem())


def call_counter(name="tool"):
    """Return (fn, calls_list). calls_list grows on every real execution."""
    calls = []
    def fn(*args, **kwargs):
        calls.append((args, kwargs))
        return f"{name}-result"
    fn.__name__ = name
    fn.__module__ = "test_ledger"
    return fn, calls


# ─── Core block / allow ───────────────────────────────────────────────────────

class TestBlock(unittest.TestCase):

    def test_first_call_executes(self):
        guard = fresh()
        fn, calls = call_counter()
        guard(fn, x=1)
        self.assertEqual(len(calls), 1)

    def test_duplicate_is_blocked(self):
        guard = fresh()
        fn, calls = call_counter()
        guard(fn, x=1)
        guard(fn, x=1)
        guard(fn, x=1)
        self.assertEqual(len(calls), 1)

    def test_different_args_both_execute(self):
        guard = fresh()
        fn, calls = call_counter()
        guard(fn, x=1)
        guard(fn, x=2)
        self.assertEqual(len(calls), 2)

    def test_returns_none_when_blocked(self):
        guard = fresh()
        fn, _ = call_counter()
        guard(fn, x=1)
        result = guard(fn, x=1)
        self.assertIsNone(result)

    def test_returns_result_on_first_call(self):
        guard = fresh()
        fn, _ = call_counter("myfn")
        result = guard(fn, x=1)
        self.assertEqual(result, "myfn-result")

    def test_arg_order_does_not_matter(self):
        """guard(fn, x=1, y=2) == guard(fn, y=2, x=1) — same fingerprint."""
        guard = fresh()
        fn, calls = call_counter()
        guard(fn, x=1, y=2)
        guard(fn, y=2, x=1)
        self.assertEqual(len(calls), 1)

    def test_positional_equals_keyword(self):
        """guard(fn, 42) == guard(fn, x=42) when signature has param x."""
        guard = fresh()
        def fn(x): return x
        fn.__module__ = "test_ledger"
        guard(fn, 42)
        guard(fn, x=42)
        s = guard.stats()
        self.assertEqual(s["executed"], 1)
        self.assertEqual(s["blocked"],  1)


# ─── Exception handling ───────────────────────────────────────────────────────

class TestFailure(unittest.TestCase):

    def test_failed_tool_is_retriable(self):
        """After a FAILED record, the next call should execute again."""
        guard = fresh()
        attempts = []

        def flaky():
            attempts.append(1)
            if len(attempts) == 1:
                raise ValueError("first attempt fails")
            return "ok"
        flaky.__module__ = "test_ledger"

        with self.assertRaises(ValueError):
            guard(flaky)
        result = guard(flaky)   # second call must execute, not be blocked
        self.assertEqual(len(attempts), 2)
        self.assertEqual(result, "ok")

    def test_exception_is_reraised(self):
        guard = fresh()
        def boom(): raise RuntimeError("boom")
        boom.__module__ = "test_ledger"
        with self.assertRaises(RuntimeError):
            guard(boom)

    def test_failed_record_shows_error(self):
        guard = fresh()
        def explode(): raise TypeError("bad type")
        explode.__module__ = "test_ledger"
        with self.assertRaises(TypeError):
            guard(explode)
        hist = guard.history()
        self.assertIn("TypeError", hist[0]["error"])


# ─── Escape hatches ───────────────────────────────────────────────────────────

class TestEscapeHatches(unittest.TestCase):

    def test_retry_clears_record(self):
        guard = fresh()
        fn, calls = call_counter()
        guard(fn, x=1)
        guard.retry(fn, x=1)
        guard(fn, x=1)
        self.assertEqual(len(calls), 2)

    def test_force_executes_regardless(self):
        guard = fresh()
        fn, calls = call_counter()
        guard(fn, x=1)
        guard.force(fn, x=1)
        self.assertEqual(len(calls), 2)

    def test_reset_clears_all(self):
        guard = fresh()
        fn, calls = call_counter()
        guard(fn, x=1)
        guard(fn, x=2)
        guard.reset()
        guard(fn, x=1)
        guard(fn, x=2)
        self.assertEqual(len(calls), 4)


# ─── key= pattern ─────────────────────────────────────────────────────────────

class TestKeyPattern(unittest.TestCase):

    def test_key_deduplicates_nondeterministic_args(self):
        """Without key=, different timestamps → different fingerprints → no dedup.
        With key=, fingerprint is (tool, key, wf) — args ignored."""
        guard = fresh()
        fn, calls = call_counter()

        # With key= — same key, different timestamps → blocked
        for i in range(4):
            guard(fn, ts=time.time() + i, key="order-42")
        self.assertEqual(len(calls), 1)

    def test_without_key_nondeterministic_args_slip_through(self):
        guard = fresh()
        fn, calls = call_counter()

        for i in range(3):
            guard(fn, ts=time.time() + i)   # no key=, every fingerprint differs
        self.assertEqual(len(calls), 3)

    def test_different_keys_both_execute(self):
        guard = fresh()
        fn, calls = call_counter()
        guard(fn, x=1, key="k1")
        guard(fn, x=1, key="k2")
        self.assertEqual(len(calls), 2)


# ─── Policy ───────────────────────────────────────────────────────────────────

class TestPolicy(unittest.TestCase):

    def test_unlimited_always_runs(self):
        guard = fresh()
        fn, calls = call_counter()
        guard.policy(fn, unlimited=True)
        for _ in range(5):
            guard(fn, x=1)
        self.assertEqual(len(calls), 5)

    def test_max_caps_executions(self):
        guard = fresh()
        fn, calls = call_counter()
        guard.policy(fn, max=2)
        for _ in range(5):
            guard(fn, x=1)
        self.assertEqual(len(calls), 2)

    def test_replay_returns_cached_result(self):
        guard = fresh()
        fn, _ = call_counter("cached")
        guard.policy(fn, replay=True)
        first  = guard(fn, x=1)
        second = guard(fn, x=1)   # blocked but should return cached
        self.assertEqual(first,  "cached-result")
        self.assertEqual(second, "cached-result")

    def test_ttl_allows_rerun_after_expiry(self):
        """A record past its ttl must be treated as new — allow another execution."""
        from datetime import datetime, timezone, timedelta
        from ledger import Record, Status

        # Unit-test Record.expired() directly — no timing dependencies
        old = Record(
            id="x", tool="t", args={}, wf="default",
            created=datetime.now(timezone.utc) - timedelta(seconds=10),
            touched=datetime.now(timezone.utc) - timedelta(seconds=10),
            status=Status.SUCCESS,
        )
        self.assertTrue(old.expired(ttl=5))     # 10s old, ttl=5  → expired
        self.assertFalse(old.expired(ttl=60))   # 10s old, ttl=60 → still valid
        self.assertFalse(old.expired(ttl=None)) # no ttl           → never expires
        self.assertFalse(old.expired(ttl=0))    # ttl=0 treated as "no ttl" by design

        # Integration: run once, backdate the record so it's past ttl, run again
        guard = fresh()
        fn, calls = call_counter()
        guard.policy(fn, ttl=5)

        guard(fn, x=1)
        self.assertEqual(len(calls), 1)

        # Age the record by 10 seconds so it's past the 5s ttl
        store = guard._engine.store
        rec = store.all()[0]
        rec.touched = datetime.now(timezone.utc) - timedelta(seconds=10)
        store.put(rec)

        guard(fn, x=1)   # engine sees expired record → deletes it → claims fresh
        self.assertEqual(len(calls), 2)

    def test_on_block_callback_fires(self):
        guard = fresh()
        fn, _ = call_counter()
        blocked_records = []
        guard.on_block(blocked_records.append)
        guard(fn, x=1)
        guard(fn, x=1)
        self.assertEqual(len(blocked_records), 1)
        self.assertEqual(blocked_records[0].tool, "test_ledger.tool")


# ─── Workflow scoping ─────────────────────────────────────────────────────────

class TestWorkflow(unittest.TestCase):

    def test_different_workflows_independent(self):
        """Same tool + same args in different workflows both execute."""
        g1 = fresh(); g1.workflow("wf-A")
        g2 = Guard(store=g1._engine.store); g2.workflow("wf-B")

        fn, calls = call_counter()
        g1(fn, x=1)
        g2(fn, x=1)
        self.assertEqual(len(calls), 2)

    def test_same_workflow_deduplicates(self):
        g1 = fresh(); g1.workflow("wf-A")
        g2 = Guard(store=g1._engine.store); g2.workflow("wf-A")

        fn, calls = call_counter()
        g1(fn, x=1)
        g2(fn, x=1)
        self.assertEqual(len(calls), 1)

    def test_reset_scoped_to_workflow(self):
        guard = fresh()
        guard.workflow("wf-A")
        fn, calls = call_counter()
        guard(fn, x=1)
        guard.reset("wf-A")
        guard(fn, x=1)
        self.assertEqual(len(calls), 2)


# ─── as_caller ────────────────────────────────────────────────────────────────

class TestCaller(unittest.TestCase):

    def test_caller_recorded(self):
        guard = fresh()
        guard.as_caller("agent-A")
        fn, _ = call_counter()
        guard(fn, x=1)
        hist = guard.history()
        self.assertEqual(hist[0]["caller"], "agent-A")

    def test_no_caller_is_none(self):
        guard = fresh()
        fn, _ = call_counter()
        guard(fn, x=1)
        self.assertIsNone(guard.history()[0]["caller"])


# ─── Decorator ────────────────────────────────────────────────────────────────

class TestDecorator(unittest.TestCase):

    def test_once_decorator_blocks_duplicates(self):
        guard = fresh()
        calls = []

        @guard.once
        def greet(name):
            calls.append(name)
            return f"hello {name}"

        greet("alice")
        greet("alice")
        self.assertEqual(len(calls), 1)

    def test_once_different_args_both_run(self):
        guard = fresh()
        calls = []

        @guard.once
        def greet(name):
            calls.append(name)

        greet("alice")
        greet("bob")
        self.assertEqual(len(calls), 2)

    def test_once_replay_returns_cached(self):
        guard = fresh()

        @guard.once(replay=True)
        def compute(x):
            return x * 10

        first  = compute(5)
        second = compute(5)
        self.assertEqual(first,  50)
        self.assertEqual(second, 50)


# ─── wrap_tools ───────────────────────────────────────────────────────────────

class TestWrapTools(unittest.TestCase):

    def test_dict_of_tools(self):
        guard = fresh()
        fn, calls = call_counter()
        tools = guard.wrap_tools({"mytool": fn})
        tools["mytool"](x=1)
        tools["mytool"](x=1)
        self.assertEqual(len(calls), 1)

    def test_list_of_tools(self):
        guard = fresh()
        fn, calls = call_counter()
        protected = guard.wrap_tools([fn])
        protected[0](x=1)
        protected[0](x=1)
        self.assertEqual(len(calls), 1)

    def test_invalid_input_raises(self):
        guard = fresh()
        with self.assertRaises(TypeError):
            guard.wrap_tools("not a dict or list")

    def test_preserves_function_name(self):
        guard = fresh()
        fn, _ = call_counter("mytool")
        protected = guard.wrap_tools([fn])
        self.assertEqual(protected[0].__name__, "mytool")


# ─── Async ────────────────────────────────────────────────────────────────────

class TestAsync(unittest.TestCase):

    def test_async_first_call_executes(self):
        guard = fresh()
        calls = []

        async def async_tool(x):
            calls.append(x)
            return f"result-{x}"
        async_tool.__module__ = "test_ledger"

        asyncio.run(guard(async_tool, x=1))
        self.assertEqual(len(calls), 1)

    def test_async_duplicate_blocked(self):
        guard = fresh()
        calls = []

        async def async_tool(x):
            calls.append(x)
            return f"result-{x}"
        async_tool.__module__ = "test_ledger"

        asyncio.run(guard(async_tool, x=1))
        asyncio.run(guard(async_tool, x=1))
        self.assertEqual(len(calls), 1)


# ─── Thread safety ────────────────────────────────────────────────────────────

class TestConcurrency(unittest.TestCase):

    def test_only_one_thread_executes(self):
        """10 threads race to call the same tool. Exactly 1 should win."""
        guard = fresh()
        fn, calls = call_counter()
        barrier = threading.Barrier(10)

        def race():
            barrier.wait()   # all threads start simultaneously
            guard(fn, x=1)

        threads = [threading.Thread(target=race) for _ in range(10)]
        for t in threads: t.start()
        for t in threads: t.join()

        self.assertEqual(len(calls), 1)

    def test_stats_are_consistent_under_concurrency(self):
        guard = fresh()
        fn, _ = call_counter()
        barrier = threading.Barrier(10)

        def race():
            barrier.wait()
            guard(fn, x=1)

        threads = [threading.Thread(target=race) for _ in range(10)]
        for t in threads: t.start()
        for t in threads: t.join()

        s = guard.stats()
        self.assertEqual(s["executed"], 1)
        self.assertEqual(s["attempts"], 10)
        self.assertEqual(s["blocked"] + s["executed"], s["attempts"])


# ─── Store protocol ───────────────────────────────────────────────────────────

class TestCustomStore(unittest.TestCase):

    def test_mem_store_satisfies_protocol(self):
        self.assertIsInstance(_Mem(), Store)

    def test_custom_store_works(self):
        """A minimal custom store (backed by a dict) must work as a drop-in."""
        class DictStore(_Mem):
            pass   # _Mem is already a correct implementation

        guard = Guard(store=DictStore())
        fn, calls = call_counter()
        guard(fn, x=1)
        guard(fn, x=1)
        self.assertEqual(len(calls), 1)


# ─── Observability ────────────────────────────────────────────────────────────

class TestObservability(unittest.TestCase):

    def test_stats_counts(self):
        guard = fresh()
        fn, _ = call_counter()
        guard(fn, x=1)
        guard(fn, x=1)
        guard(fn, x=2)
        s = guard.stats()
        self.assertEqual(s["actions"],  2)
        self.assertEqual(s["attempts"], 3)
        self.assertEqual(s["executed"], 2)
        self.assertEqual(s["blocked"],  1)

    def test_history_returns_dicts(self):
        guard = fresh()
        fn, _ = call_counter()
        guard(fn, x=1)
        hist = guard.history()
        self.assertEqual(len(hist), 1)
        self.assertIn("tool",     hist[0])
        self.assertIn("status",   hist[0])
        self.assertIn("attempts", hist[0])

    def test_history_filtered_by_tool(self):
        guard = fresh()
        fn1, _ = call_counter("alpha")
        fn2, _ = call_counter("beta")
        guard(fn1, x=1)
        guard(fn2, x=1)
        hist = guard.history(fn1)
        self.assertEqual(len(hist), 1)
        self.assertIn("alpha", hist[0]["tool"])

    def test_quiet_suppresses_output(self, capsys=None):
        """guard.quiet() should not raise — output suppression tested manually."""
        guard = fresh()
        guard.quiet()
        fn, calls = call_counter()
        guard(fn, x=1)
        self.assertEqual(len(calls), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
