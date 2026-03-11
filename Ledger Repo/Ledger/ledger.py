"""
ledger.py — AI agents retry constantly. Ledger stops the damage.

ZERO CONFIG — works out of the box, persists automatically, no setup needed:

    from ledger import guard

    guard(send_email, to="user@example.com")   # runs
    guard(send_email, to="user@example.com")   # blocked — already sent
    guard(send_email, to="user@example.com")   # blocked — already sent

    # Console output (on by default so you can see it working):
    # [ledger] ✓ send_email
    # [ledger] ✗ send_email (blocked — already ran at 14:23:01)
    # [ledger] ✗ send_email (blocked — already ran at 14:23:01)

    # ledger.db is created automatically in your current directory.
    # Records survive restarts. No configuration required.

WRAP AN ENTIRE TOOLSET (agent frameworks):
    tools = guard.wrap_tools(tools)   # dict or list — every tool auto-protected

ENVIRONMENT VARIABLES (optional overrides):
    LEDGER_DB          path to the database file  (default: ./ledger.db)
    LEDGER_WORKFLOW    workflow scope id           (default: "default")
    LEDGER_QUIET       set to "1" to silence output

PER-TOOL RULES:
    guard.policy(search,  unlimited=True)   # reads: always run
    guard.policy(refund,  replay=True)      # writes: block + replay cached result
    guard.policy(sms,     max=2)            # notifications: cap at 2

CUSTOM IDEMPOTENCY KEY (mirrors Stripe's idempotency-key header):
    guard(charge_card, amount=99, key="order-9981")

ASYNC:
    result = await guard(post_webhook, url="...", payload=data)

DECORATOR:
    @guard.once
    def charge_card(card_id, amount): ...

ESCAPE HATCHES:
    guard.retry(send_email, to="user@example.com")  # clear record, allow next call
    guard.force(send_email, to="user@example.com")  # run right now regardless

SEE WHAT HAPPENED:
    guard.log()
    # ✓ send_email    attempts 3   executed 1   blocked 2   <- retried 2x
    # ✗ charge_card   attempts 1   executed 0               -> CardError: declined

SWAP STORAGE BACKEND (Redis, Postgres, etc.):
    from ledger import Store, Guard
    class RedisStore(Store):
        def get(self, id): ...
        def claim(self, r): ...
        def put(self, r): ...
        def delete(self, id): ...
        def all(self, wf=None): ...
        def clear(self, wf=None): ...

    guard = Guard(store=RedisStore())

CLI:
    python -m ledger_cli show  ledger.db   # full history
    python -m ledger_cli tail  ledger.db   # live-tail
    python -m ledger_cli clear ledger.db   # wipe records
    python -m ledger_cli stats ledger.db   # summary
"""

from __future__ import annotations

import asyncio
import functools
import hashlib
import inspect
import json
import logging
import os
import sqlite3
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Callable, Protocol, runtime_checkable

_log = logging.getLogger("ledger")

__version__ = "0.1.0"

__all__ = ["Guard", "guard", "Store", "Record", "Status", "Policy"]

# ── Defaults read from environment ────────────────────────────────────────────
_DEFAULT_DB       = os.environ.get("LEDGER_DB",      "ledger.db")
_DEFAULT_WORKFLOW = os.environ.get("LEDGER_WORKFLOW", "default")
_DEFAULT_QUIET    = os.environ.get("LEDGER_QUIET",    "0") == "1"


# ── State machine ─────────────────────────────────────────────────────────────
#
#   (new) → RUNNING → SUCCESS   normal path
#                  ↘ FAILED     tool raised; safe to retry

class Status(str, Enum):
    RUNNING = "running"
    SUCCESS = "success"
    FAILED  = "failed"


@dataclass
class Record:
    id:       str                    # fingerprint
    tool:     str                    # fully-qualified function name
    args:     dict                   # normalized call arguments
    wf:       str                    # workflow scope
    created:  datetime
    status:   Status = Status.RUNNING
    attempts: int    = 0             # total times agent tried (including duplicates)
    runs:     int    = 0             # times the tool actually executed
    blocked:  int    = 0             # times Ledger stopped a duplicate
    result:   Any    = None          # cached return value (for replay)
    error:    str | None = None
    caller:   str | None = None      # optional identity — agent name, user id, etc.
    since:    datetime | None = None # RUNNING start time (crash detection)
    touched:  datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def stale(self, timeout: int) -> bool:
        """True if stuck RUNNING longer than `timeout` seconds.

        Indicates the process that claimed this record crashed mid-execution.
        Set timeout conservatively — higher than your P99 tool latency (default 300s).

        Edge case: if the original process is still running but very slow and the
        stale timeout fires, two processes could execute the same tool concurrently.
        This is an inherent tradeoff in any crash-recovery idempotency system.
        """
        if self.status != Status.RUNNING or not self.since:
            return False
        return (datetime.now(timezone.utc) - self.since).total_seconds() > timeout

    def expired(self, ttl: int | None) -> bool:
        """True if record is older than `ttl` seconds — allow fresh execution."""
        if not ttl:
            return False
        return (datetime.now(timezone.utc) - self.touched).total_seconds() > ttl

    def as_dict(self) -> dict[str, Any]:
        return dict(
            tool=self.tool, args=self.args, wf=self.wf,
            status=self.status.value, attempts=self.attempts,
            runs=self.runs, blocked=self.blocked, error=self.error,
            caller=self.caller,
            created=self.created.isoformat(), touched=self.touched.isoformat(),
        )

    def __repr__(self) -> str:
        caller = f" caller={self.caller!r}" if self.caller else ""
        return (
            f"<Record {self.tool}{caller} "
            f"attempts={self.attempts} runs={self.runs} blocked={self.blocked} "
            f"status={self.status.value}>"
        )


# ── Fingerprint ───────────────────────────────────────────────────────────────

def _fp(tool: str, args: dict, wf: str, key: str | None = None) -> str:
    """Stable 32-char hex fingerprint (128 bits — collision-safe at any call volume).

    If `key` is given, fingerprint is (tool, key, wf) — args are ignored entirely.
    Use this when args contain non-deterministic values (timestamps, UUIDs):
        guard(send_email, to="user", ts=time.now(), key=f"email-{order_id}")

    Otherwise fingerprint is (tool, normalized_args, wf).
    Arg order doesn't matter: guard(fn, x=1, y=2) == guard(fn, y=2, x=1).
    Positional == keyword:   guard(fn, 42)       == guard(fn, x=42).
    Float drift handled:     guard(fn, x=99.99)  == guard(fn, x=99.9900000001).
    """
    if key is not None:
        payload = json.dumps({"t": tool, "k": key, "w": wf},
                             sort_keys=True, separators=(",", ":"))
    else:
        payload = json.dumps({"t": tool, "a": _norm(args), "w": wf},
                             sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()[:32]


def _norm(v: Any) -> Any:
    """Recursively normalize so key order and float precision never affect the fingerprint."""
    if isinstance(v, dict):
        return {k: _norm(vv) for k, vv in sorted(v.items())}
    if isinstance(v, (list, tuple)):
        return [_norm(i) for i in v]
    if isinstance(v, float):
        return round(v, 8)
    if isinstance(v, (str, int, bool)) or v is None:
        return v
    return str(v)


_SIG_CACHE: dict[Callable, inspect.Signature] = {}


def _bind(fn: Callable, args: tuple, kwargs: dict) -> dict:
    """Map positional args to param names via the function signature.

    Caches signatures — inspect.signature() is slow; caching makes hot paths ~10x faster.
    Falls back gracefully if binding fails (e.g. *args-only functions).
    """
    try:
        sig = _SIG_CACHE.get(fn)
        if sig is None:
            sig = inspect.signature(fn)
            _SIG_CACHE[fn] = sig
        b = sig.bind(*args, **kwargs)
        b.apply_defaults()
        return dict(b.arguments)
    except (ValueError, TypeError):
        return {**kwargs, "_": list(args)} if args else dict(kwargs)


def _tool_name(fn: Callable) -> str:
    """Fully-qualified tool name used as the policy lookup key."""
    return f"{fn.__module__}.{fn.__name__}"


def _short_name(tool: str) -> str:
    """Last segment of a dotted tool name — for readable console output."""
    return tool.split(".")[-1]


# ── Store protocol ────────────────────────────────────────────────────────────
#
# Any class that implements these six methods can be used as a Ledger backend.
# Drop-in replacements for Redis, Postgres, DynamoDB, etc.:
#
#   class RedisStore(Store):
#       def get(self, id): ...      # return Record or None
#       def claim(self, r): ...     # atomic insert-if-absent; return True if won
#       def put(self, r): ...       # upsert
#       def delete(self, id): ...   # remove by id
#       def all(self, wf=None): ... # return list[Record], optionally filtered by wf
#       def clear(self, wf=None):   # delete all, or only records for wf
#
#   guard = Guard(store=RedisStore())

@runtime_checkable
class Store(Protocol):
    def get(self, id: str) -> Record | None: ...
    def claim(self, r: Record) -> bool: ...
    def put(self, r: Record) -> None: ...
    def delete(self, id: str) -> None: ...
    def all(self, wf: str | None = None) -> list[Record]: ...
    def clear(self, wf: str | None = None) -> None: ...


# ── Storage implementations ───────────────────────────────────────────────────

class _Mem:
    """In-memory store. Thread-safe via lock. Lost on restart."""

    def __init__(self) -> None:
        self._d: dict[str, Record] = {}
        self._lock = threading.Lock()

    def get(self, id: str) -> Record | None:
        with self._lock:
            return self._d.get(id)

    def claim(self, r: Record) -> bool:
        with self._lock:
            if r.id in self._d:
                return False
            self._d[r.id] = r
            return True

    def put(self, r: Record) -> None:
        with self._lock:
            self._d[r.id] = r

    def delete(self, id: str) -> None:
        with self._lock:
            self._d.pop(id, None)

    def all(self, wf: str | None = None) -> list[Record]:
        with self._lock:
            recs = list(self._d.values())
        return [r for r in recs if r.wf == wf] if wf else recs

    def clear(self, wf: str | None = None) -> None:
        with self._lock:
            if wf:
                self._d = {k: v for k, v in self._d.items() if v.wf != wf}
            else:
                self._d.clear()


class _SQLite:
    """File-backed store. Survives restarts. No extra infrastructure.

    One SQLite connection per thread (SQLite constraint).
    INSERT OR IGNORE makes claim() atomic — safe across threads and processes.

    Concurrency: WAL mode handles dozens of concurrent writers cleanly.
    At very high write throughput (100+ workers), swap this for a Redis or
    Postgres implementation using the Store protocol above.
    """

    def __init__(self, path: str) -> None:
        self.path = str(path)
        self._tl  = threading.local()
        self._init()

    def _conn(self) -> sqlite3.Connection:
        if not hasattr(self._tl, "c"):
            c = sqlite3.connect(self.path, check_same_thread=False, timeout=30)
            c.execute("PRAGMA journal_mode=WAL")
            c.execute("PRAGMA synchronous=NORMAL")
            c.row_factory = sqlite3.Row
            self._tl.c = c
        return self._tl.c

    def _init(self) -> None:
        with self._conn() as c:
            c.execute("""
                CREATE TABLE IF NOT EXISTS ledger (
                    id       TEXT PRIMARY KEY,
                    tool     TEXT,
                    args     TEXT,
                    wf       TEXT,
                    created  TEXT,
                    touched  TEXT,
                    since    TEXT,
                    status   TEXT DEFAULT 'running',
                    attempts INT  DEFAULT 0,
                    runs     INT  DEFAULT 0,
                    blocked  INT  DEFAULT 0,
                    result   TEXT,
                    error    TEXT,
                    caller   TEXT
                )
            """)
            # Add caller column to existing databases that pre-date this field
            try:
                c.execute("ALTER TABLE ledger ADD COLUMN caller TEXT")
            except sqlite3.OperationalError:
                pass  # column already exists
            c.execute("CREATE INDEX IF NOT EXISTS i_wf     ON ledger(wf)")
            c.execute("CREATE INDEX IF NOT EXISTS i_caller ON ledger(caller)")

    def get(self, id: str) -> Record | None:
        row = self._conn().execute("SELECT * FROM ledger WHERE id=?", (id,)).fetchone()
        return self._row(row) if row else None

    def claim(self, r: Record) -> bool:
        try:
            with self._conn() as c:
                c.execute(
                    """INSERT OR IGNORE INTO ledger
                       (id,tool,args,wf,created,touched,since,status,
                        attempts,runs,blocked,result,error,caller)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    self._vals(r),
                )
                return c.execute("SELECT changes()").fetchone()[0] > 0
        except sqlite3.IntegrityError:
            return False

    def put(self, r: Record) -> None:
        with self._conn() as c:
            c.execute(
                """UPDATE ledger
                   SET touched=?, since=?, status=?,
                       attempts=?, runs=?, blocked=?, result=?, error=?, caller=?
                   WHERE id=?""",
                (
                    r.touched.isoformat(),
                    r.since.isoformat() if r.since else None,
                    r.status.value, r.attempts, r.runs, r.blocked,
                    json.dumps(r.result) if r.result is not None else None,
                    r.error, r.caller, r.id,
                ),
            )

    def delete(self, id: str) -> None:
        with self._conn() as c:
            c.execute("DELETE FROM ledger WHERE id=?", (id,))

    def all(self, wf: str | None = None) -> list[Record]:
        q    = "SELECT * FROM ledger WHERE wf=?" if wf else "SELECT * FROM ledger ORDER BY created"
        rows = self._conn().execute(q, (wf,) if wf else ()).fetchall()
        return [self._row(r) for r in rows]

    def clear(self, wf: str | None = None) -> None:
        with self._conn() as c:
            if wf:
                c.execute("DELETE FROM ledger WHERE wf=?", (wf,))
            else:
                c.execute("DELETE FROM ledger")

    def _vals(self, r: Record) -> tuple:
        return (
            r.id, r.tool, json.dumps(r.args), r.wf,
            r.created.isoformat(), r.touched.isoformat(),
            r.since.isoformat() if r.since else None,
            r.status.value, r.attempts, r.runs, r.blocked,
            json.dumps(r.result) if r.result is not None else None,
            r.error, r.caller,
        )

    @staticmethod
    def _row(row: sqlite3.Row) -> Record:
        cols = row.keys()
        return Record(
            id=row["id"],
            tool=row["tool"],
            args=json.loads(row["args"]),
            wf=row["wf"],
            created=datetime.fromisoformat(row["created"]),
            touched=datetime.fromisoformat(row["touched"]),
            since=datetime.fromisoformat(row["since"]) if row["since"] else None,
            status=Status(row["status"]),
            attempts=row["attempts"],
            runs=row["runs"],
            blocked=row["blocked"],
            result=json.loads(row["result"]) if row["result"] else None,
            error=row["error"],
            caller=row["caller"] if "caller" in cols else None,
        )


# ── Policy ────────────────────────────────────────────────────────────────────

@dataclass
class Policy:
    max:     int | None = 1      # None = unlimited executions
    replay:  bool       = False  # return cached result to blocked callers
    ttl:     int | None = None   # seconds before record expires; allow again
    timeout: int        = 300    # seconds before RUNNING = crashed; allow retry


# ── Engine ────────────────────────────────────────────────────────────────────

class _Engine:
    def __init__(
        self,
        store:  Store | None = None,
        wf:     str  = _DEFAULT_WORKFLOW,
        quiet:  bool = _DEFAULT_QUIET,
        caller: str | None = None,
    ) -> None:
        self.store     = store or _SQLite(_DEFAULT_DB)
        self.wf        = wf
        self.caller    = caller
        self.policies: dict[str, Policy] = {}
        self.default   = Policy()
        self.quiet     = quiet
        self._on_block: Callable[[Record], None] | None = None
        # key= footgun detection: track recent unique fingerprints per tool
        # {tool_name: [(fingerprint, timestamp), ...]}
        self._recent_fps: dict[str, list[tuple[str, float]]] = {}

    def check_and_claim(
        self,
        tool: str,
        args: dict,
        wf:   str,
        key:  str | None = None,
    ) -> tuple[bool, Record, Any]:
        """Evaluate policy → claim if allowed → return (allowed, record, cached_result)."""
        p   = self.policies.get(tool, self.default)
        fp  = _fp(tool, args, wf, key)
        now = datetime.now(timezone.utc)
        rec = self.store.get(fp)

        # ── key= footgun detection ────────────────────────────────────────────
        # If the same tool is called with 3+ different fingerprints within 10s
        # and no key= was provided, the user almost certainly has non-deterministic
        # args (timestamps, UUIDs) and Ledger cannot deduplicate them.
        if key is None and not self.quiet and p.max is not None:
            import time as _time
            now_ts = _time.monotonic()
            bucket = self._recent_fps.setdefault(tool, [])
            # Evict entries older than 10 seconds
            bucket[:] = [(f, t) for f, t in bucket if now_ts - t < 10.0]
            if fp not in {f for f, _ in bucket}:
                bucket.append((fp, now_ts))
            if len(bucket) >= 3:
                print(
                    f"[ledger] ⚠  {_short_name(tool)} called {len(bucket)}× with different "
                    f"args in <10s — if retrying, add key= to deduplicate:\n"
                    f"           guard({_short_name(tool)}, ..., key=\"your-stable-id\")"
                )

        # ── Invalidate records that should be treated as new ─────────────────
        if rec:
            if rec.expired(p.ttl) or rec.stale(p.timeout):
                self.store.delete(fp)
                rec = None
            elif rec.status == Status.FAILED:
                self.store.delete(fp)
                rec = None

        # ── Block: SUCCESS record has hit its execution limit ─────────────────
        prior_runs     = 0
        prior_attempts = 0
        prior_blocked  = 0

        if rec and rec.status == Status.SUCCESS:
            if p.max is None or rec.runs < p.max:
                # Under limit — carry counts forward and allow another run
                prior_runs     = rec.runs
                prior_attempts = rec.attempts
                prior_blocked  = rec.blocked
                self.store.delete(fp)
                rec = None
            else:
                rec.blocked  += 1
                rec.attempts += 1
                rec.touched   = now
                self.store.put(rec)
                self._emit_blocked(tool, rec.blocked, rec.touched)
                self._fire_block(rec)
                return False, rec, (rec.result if p.replay else None)

        # ── Block: concurrent RUNNING ─────────────────────────────────────────
        if rec and rec.status == Status.RUNNING:
            rec.blocked  += 1
            rec.attempts += 1
            rec.touched   = now
            self.store.put(rec)
            self._emit_blocked(tool, rec.blocked, rec.since or rec.touched)
            self._fire_block(rec)
            return False, rec, None

        # ── Claim ─────────────────────────────────────────────────────────────

        new = Record(
            id=fp, tool=tool, args=args, wf=wf,
            created=now, touched=now, since=now,
            status=Status.RUNNING,
            attempts=prior_attempts + 1,
            runs=prior_runs,
            blocked=prior_blocked,
            caller=self.caller,
        )
        if not self.store.claim(new):
            return False, self.store.get(fp) or new, None

        self._emit_allowed(tool)
        return True, new, None

    def succeed(self, rec: Record, result: Any) -> None:
        rec.status  = Status.SUCCESS
        rec.result  = result
        rec.runs   += 1
        rec.since   = None
        rec.touched = datetime.now(timezone.utc)
        self.store.put(rec)

    def fail(self, rec: Record, error: Exception) -> None:
        rec.status  = Status.FAILED
        rec.error   = f"{type(error).__name__}: {error}"
        rec.since   = None
        rec.touched = datetime.now(timezone.utc)
        self.store.put(rec)

    # ── Console output ────────────────────────────────────────────────────────
    #
    # Printed directly to stdout (not via logging) so beginners see it
    # immediately without configuring a log handler. Use .quiet() to silence.

    def _emit_allowed(self, tool: str) -> None:
        if not self.quiet:
            print(f"[ledger] ✓ {_short_name(tool)}")

    def _emit_blocked(self, tool: str, count: int, ran_at: datetime) -> None:
        ts = ran_at.astimezone().strftime("%H:%M:%S")
        if not self.quiet:
            print(f"[ledger] ✗ {_short_name(tool)} (blocked — already ran at {ts})")
        _log.debug("[ledger] ✗ %s blocked %dx", tool, count)

    def _fire_block(self, rec: Record) -> None:
        if self._on_block:
            try:
                self._on_block(rec)
            except Exception:
                pass


# ── Guard ─────────────────────────────────────────────────────────────────────

class Guard:
    """Idempotency guard for AI agent tool calls.

    Works out of the box with zero configuration:

        from ledger import guard
        guard(send_email, to="user@example.com")

    Records are automatically persisted to ./ledger.db so nothing is lost
    on restart. Output is printed to the console so you can see it working.

    For agent frameworks, protect all tools at once:

        tools = guard.wrap_tools(tools)

    Custom storage backend (Redis, Postgres, etc.):

        guard = Guard(store=MyStore())

    Environment variables (all optional):
        LEDGER_DB          path to the database    (default: ./ledger.db)
        LEDGER_WORKFLOW    workflow scope id        (default: "default")
        LEDGER_QUIET       set "1" to silence       (default: output on)

    Full API
    ────────
    guard(fn, **kwargs)          run once; block duplicates
    await guard(fn, **kwargs)    async — same guarantees
    @guard.once                  decorator — protect every call site
    guard.wrap_tools(tools)      auto-protect a dict or list of tools
    guard.policy(fn, ...)        per-tool execution rules
    guard.workflow("run-123")    scope to a specific run or order
    guard.as_caller("agent-A")   tag records with an identity
    guard.persist("path.db")     override the database path in code
    guard.retry(fn, ...)         clear a record; allow the next call through
    guard.force(fn, ...)         execute immediately regardless of history
    guard.log()                  print a summary of all recorded actions
    guard.stats()                return counts as a dict
    guard.reset()                clear all records (useful in tests)
    guard.quiet()                silence console output
    """

    def __init__(self, store: Store | None = None) -> None:
        self._engine = _Engine(store=store)

    # ── Core call ─────────────────────────────────────────────────────────────

    def __call__(self, fn: Callable, /, *args: Any, **kwargs: Any) -> Any:
        """Run `fn` once per unique (fn, args, workflow) combination.

        Blocks duplicate calls automatically. Works with both sync and async
        functions — detection is automatic, no extra syntax needed.

        Non-deterministic arguments
        ───────────────────────────
        If args contain timestamps, UUIDs, or random values, every call gets a
        different fingerprint and Ledger cannot detect duplicates. Fix this with
        a stable `key=` based on your business identity:

            # Bad  — timestamp makes every call unique
            guard(send_email, to="user@x.com", sent_at=time.now())

            # Good — key is stable; non-deterministic args are ignored
            guard(send_email, to="user@x.com", sent_at=time.now(),
                  key=f"email-{order_id}")
        """
        key = kwargs.pop("key", None)
        if asyncio.iscoroutinefunction(fn):
            return self._acall(fn, args, kwargs, key)
        return self._call(fn, args, kwargs, key)

    def _call(self, fn: Callable, args: tuple, kwargs: dict, key: str | None = None) -> Any:
        fargs = _bind(fn, args, kwargs)
        tool  = _tool_name(fn)
        ok, rec, cached = self._engine.check_and_claim(tool, fargs, self._engine.wf, key)
        if not ok:
            return cached
        try:
            result = fn(*args, **kwargs)
            self._engine.succeed(rec, result)
            return result
        except Exception as exc:
            self._engine.fail(rec, exc)
            raise

    async def _acall(
        self, fn: Callable, args: tuple, kwargs: dict, key: str | None = None
    ) -> Any:
        fargs = _bind(fn, args, kwargs)
        tool  = _tool_name(fn)
        ok, rec, cached = self._engine.check_and_claim(tool, fargs, self._engine.wf, key)
        if not ok:
            return cached
        try:
            result = await fn(*args, **kwargs)
            self._engine.succeed(rec, result)
            return result
        except Exception as exc:
            self._engine.fail(rec, exc)
            raise

    # ── Decorator ─────────────────────────────────────────────────────────────

    def once(
        self,
        fn: Callable | None = None,
        *,
        replay: bool = False,
    ) -> Callable:
        """Protect a function so it executes at most once per unique call.

            @guard.once
            def send_email(to, subject): ...

            @guard.once(replay=True)   # blocked callers receive the cached result
            def create_invoice(id, amount): ...
        """
        def wrap(f: Callable) -> Callable:
            if replay:
                self._engine.policies[_tool_name(f)] = Policy(max=1, replay=True)

            @functools.wraps(f)
            def sync_wrapper(*a: Any, **kw: Any) -> Any:
                return self._call(f, a, kw)

            @functools.wraps(f)
            async def async_wrapper(*a: Any, **kw: Any) -> Any:
                return await self._acall(f, a, kw)

            return async_wrapper if asyncio.iscoroutinefunction(f) else sync_wrapper

        return wrap(fn) if fn is not None else wrap  # type: ignore[return-value]

    # ── Tool wrapping ─────────────────────────────────────────────────────────

    def wrap_tools(
        self,
        tools: dict[str, Callable] | list[Callable],
    ) -> dict[str, Callable] | list[Callable]:
        """Automatically protect an entire toolset — no per-call guard() needed.

            tools = guard.wrap_tools({
                "send_email":  send_email,
                "charge_card": charge_card,
            })

            tools = guard.wrap_tools([send_email, charge_card])

        Returns the same collection type that was passed in.
        """
        if isinstance(tools, dict):
            return {name: self._wrap_one(fn) for name, fn in tools.items()}
        if isinstance(tools, list):
            return [self._wrap_one(fn) for fn in tools]
        raise TypeError(
            f"wrap_tools() expects a dict or list of callables, "
            f"got {type(tools).__name__!r}.\n"
            "  dict example: {'send_email': send_email, 'charge_card': charge_card}\n"
            "  list example: [send_email, charge_card]"
        )

    def _wrap_one(self, fn: Callable) -> Callable:
        if not callable(fn):
            raise TypeError(
                f"wrap_tools() expected a callable, "
                f"got {type(fn).__name__!r} (value: {fn!r})"
            )
        if asyncio.iscoroutinefunction(fn):
            @functools.wraps(fn)
            async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
                key = kwargs.pop("key", None)
                return await self._acall(fn, args, kwargs, key)
            async_wrapper.__wrapped__ = fn  # type: ignore[attr-defined]
            return async_wrapper
        else:
            @functools.wraps(fn)
            def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
                key = kwargs.pop("key", None)
                return self._call(fn, args, kwargs, key)
            sync_wrapper.__wrapped__ = fn  # type: ignore[attr-defined]
            return sync_wrapper

    # ── Configuration ─────────────────────────────────────────────────────────

    def persist(self, path: str = "ledger.db") -> "Guard":
        """Override the database path in code (env var LEDGER_DB also works).

            guard.persist("/data/myagent.db")
        """
        self._engine.store = _SQLite(str(path))
        return self

    def workflow(self, id: str) -> "Guard":
        """Scope deduplication to a specific run, request, or order.

            guard.workflow(f"order-{order_id}")

        Can also be set via the LEDGER_WORKFLOW environment variable.
        """
        self._engine.wf = id
        return self

    def as_caller(self, caller: str) -> "Guard":
        """Tag all records created by this guard instance with an identity.

        Use this to track which agent, user, or service performed each action.
        Visible in the dashboard and included in every record:

            guard.as_caller("agent-A")
            guard.as_caller(f"user-{user_id}")
        """
        self._engine.caller = caller
        return self

    def policy(
        self,
        tool: Callable | str,
        *,
        unlimited: bool = False,
        replay:    bool = False,
        max:       int | None = None,
        ttl:       int | None = None,
    ) -> "Guard":
        """Set per-tool execution rules. Call before your agent runs.

            guard.policy(search,  unlimited=True)   # reads: always run
            guard.policy(refund,  replay=True)       # writes: block + return cached result
            guard.policy(sms,     max=2)             # cap at 2 executions
            guard.policy(report,  ttl=86400)         # once per day
        """
        name = _tool_name(tool) if callable(tool) else tool
        if unlimited:
            p = Policy(max=None)
        elif max is not None:
            p = Policy(max=max, replay=replay, ttl=ttl)
        elif ttl is not None:
            p = Policy(max=1, replay=replay, ttl=ttl)
        else:
            p = Policy(max=1, replay=replay)
        self._engine.policies[name] = p
        return self

    def on_block(self, fn: Callable[[Record], None]) -> "Guard":
        """Register a callback fired on every blocked duplicate call.

            guard.on_block(lambda r: metrics.increment(
                "ledger.blocked", tags={"tool": r.tool}
            ))
        """
        self._engine._on_block = fn
        return self

    def quiet(self) -> "Guard":
        """Silence console output. guard.log() still works.

        Can also be set via the LEDGER_QUIET=1 environment variable.
        """
        self._engine.quiet = True
        return self

    # ── Escape hatches ────────────────────────────────────────────────────────

    def retry(self, fn: Callable, /, *args: Any, **kwargs: Any) -> None:
        """Clear a record so the next call to `fn` with these args executes."""
        key  = kwargs.pop("key", None)
        tool = _tool_name(fn)
        self._engine.store.delete(_fp(tool, _bind(fn, args, kwargs), self._engine.wf, key))

    def force(self, fn: Callable, /, *args: Any, **kwargs: Any) -> Any:
        """Execute `fn` immediately regardless of Ledger history."""
        self.retry(fn, *args, **kwargs)
        return self(fn, *args, **kwargs)

    # ── Observability ─────────────────────────────────────────────────────────

    def log(self, wf: str | None = None) -> None:
        """Print a full action log showing attempts, executions, and blocks."""
        records = self._engine.store.all(wf)
        if not records:
            print("[ledger] nothing recorded yet")
            return
        print()
        for r in records:
            icon = "✓" if r.status == Status.SUCCESS else "✗" if r.status == Status.FAILED else "⟳"
            note = f"   <- retried {r.blocked}x" if r.blocked else ""
            caller = f"  [{r.caller}]" if r.caller else ""
            print(
                f"  {icon} {_short_name(r.tool):<24}  attempts {r.attempts:<3}  "
                f"executed {r.runs:<3}  blocked {r.blocked:<3}{note}{caller}"
            )
            if r.error:
                print(f"    -> {r.error[:72]}")
        print()

    def stats(self, wf: str | None = None) -> dict[str, int]:
        """Return aggregate counts across all recorded actions."""
        recs = self._engine.store.all(wf)
        return dict(
            actions =len(recs),
            attempts=sum(r.attempts for r in recs),
            executed=sum(r.runs     for r in recs),
            blocked =sum(r.blocked  for r in recs),
            failed  =sum(1 for r in recs if r.status == Status.FAILED),
        )

    def history(
        self,
        tool: Callable | str | None = None,
        wf:   str | None = None,
    ) -> list[dict[str, Any]]:
        """Return the full record history, optionally filtered by tool and/or workflow."""
        name = _tool_name(tool) if callable(tool) else tool
        recs = self._engine.store.all(wf)
        if name:
            recs = [r for r in recs if r.tool == name]
        return [r.as_dict() for r in recs]

    def reset(self, wf: str | None = None) -> None:
        """Clear all records so actions can execute again.

        Scoped to `wf` when provided, otherwise clears everything.
        Primary use case is test teardown:

            def teardown():
                guard.reset()
        """
        self._engine.store.clear(wf)


# ── Global singleton ──────────────────────────────────────────────────────────
#
#     from ledger import guard
#     guard(send_email, to="user@example.com")
#
# ledger.db is created automatically. Records survive restarts.
# Override via env: LEDGER_DB=./data/agent.db LEDGER_WORKFLOW=run-42 python agent.py

guard = Guard()
