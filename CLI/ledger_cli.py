"""
ledger_cli.py — CLI for inspecting and managing Ledger databases.

Usage:
    python ledger_cli.py show  ledger.db              # full history table
    python ledger_cli.py show  ledger.db --wf order-1 # filter by workflow
    python ledger_cli.py tail  ledger.db              # live-tail new activity
    python ledger_cli.py tail  ledger.db --wf order-1 # tail one workflow
    python ledger_cli.py stats ledger.db              # summary numbers
    python ledger_cli.py stats ledger.db --wf order-1 # stats for one workflow
    python ledger_cli.py clear ledger.db              # wipe all records (prompts)
    python ledger_cli.py clear ledger.db --wf order-1 # wipe one workflow (prompts)
    python ledger_cli.py clear ledger.db --yes        # skip confirmation

Install as a named command via pyproject.toml:
    [project.scripts]
    ledger = "ledger_cli:main"
"""

from __future__ import annotations

import os
import sys
import time

from ledger import _SQLite, Status, _short_name, __version__


USAGE = """\
usage: ledger_cli <command> <ledger.db> [options]

commands:
  show   ledger.db [--wf WORKFLOW]         print full history table
  tail   ledger.db [--wf WORKFLOW]         live-tail new activity  (Ctrl-C to stop)
  stats  ledger.db [--wf WORKFLOW]         summary numbers
  clear  ledger.db [--wf WORKFLOW] [--yes] wipe records (prompts for confirmation)

flags:
  --version    print version and exit
  -h, --help   print this help and exit
"""


def _icon(status: Status) -> str:
    return "✓" if status == Status.SUCCESS else "✗" if status == Status.FAILED else "⟳"


def _err(msg: str) -> None:
    """Print an error to stderr — keeps stdout clean for piping."""
    print(msg, file=sys.stderr)


# ── Commands ──────────────────────────────────────────────────────────────────

def cmd_show(store: _SQLite, wf: str | None = None) -> None:
    records = store.all(wf)
    if not records:
        scope = f" for workflow '{wf}'" if wf else ""
        print(f"[ledger] no records{scope}")
        return

    print()
    print(f"  {'TOOL':<26}  {'STATUS':<8}  {'ATT':>4}  {'RUN':>4}  {'BLK':>4}  NOTE")
    print(f"  {'─'*26}  {'─'*8}  {'─'*4}  {'─'*4}  {'─'*4}  {'─'*40}")
    for r in records:
        note = ""
        if r.error:
            note = r.error[:60]
        elif r.blocked:
            note = f"blocked {r.blocked}× of {r.attempts} attempts"
        print(
            f"  {_icon(r.status)} {_short_name(r.tool):<25}  {r.status.value:<8}  "
            f"{r.attempts:>4}  {r.runs:>4}  {r.blocked:>4}  {note}"
        )
    print(f"\n  {len(records)} record{'s' if len(records)!=1 else ''}", end="")
    if wf:
        print(f"  (workflow: {wf})", end="")
    print("\n")


def cmd_tail(store: _SQLite, path: str, wf: str | None = None) -> None:
    scope = f" [{wf}]" if wf else ""
    print(f"[ledger] tailing {path}{scope}  (Ctrl-C to stop)\n")

    # Seed with everything already in the db — only print NEW activity going forward
    seen: dict[str, int] = {r.id: r.attempts for r in store.all(wf)}
    if seen:
        print(f"  (skipping {len(seen)} existing record{'s' if len(seen)!=1 else ''} — showing new activity only)\n")

    try:
        while True:
            for r in store.all(wf):
                ts   = r.touched.astimezone().strftime("%H:%M:%S")
                name = _short_name(r.tool)

                if r.id not in seen:
                    # Brand new record
                    seen[r.id] = r.attempts
                    note   = f"  ← blocked {r.blocked}× so far" if r.blocked else ""
                    caller = f"  [{r.caller}]" if r.caller else ""
                    print(
                        f"  {ts}  {_icon(r.status)} {name:<26}  "
                        f"att={r.attempts}  run={r.runs}  blk={r.blocked}{note}{caller}"
                    )
                    if r.error:
                        print(f"          ✗ {r.error[:80]}")

                elif r.attempts != seen[r.id]:
                    # Existing record updated — new attempt came in
                    delta = r.attempts - seen[r.id]
                    seen[r.id] = r.attempts
                    note = f"  ← +{delta} attempt{'s' if delta!=1 else ''}" if delta > 1 else ""
                    print(
                        f"  {ts}  ↺ {name:<26}  "
                        f"att={r.attempts}  run={r.runs}  blk={r.blocked}{note}"
                    )
                    if r.error and r.status == Status.FAILED:
                        print(f"          ✗ {r.error[:80]}")

            time.sleep(2)
    except KeyboardInterrupt:
        print("\n[ledger] stopped")


def cmd_stats(store: _SQLite, wf: str | None = None) -> None:
    recs = store.all(wf)
    if not recs:
        scope = f" for workflow '{wf}'" if wf else ""
        print(f"[ledger] no records{scope}")
        return

    total_att   = sum(r.attempts for r in recs)
    total_run   = sum(r.runs     for r in recs)
    total_blk   = sum(r.blocked  for r in recs)
    total_fail  = sum(1 for r in recs if r.status == Status.FAILED)
    total_stuck = sum(1 for r in recs if r.status == Status.RUNNING)

    scope = f"  (workflow: {wf})" if wf else ""
    print(f"\n  actions  : {len(recs)}{scope}")
    print(f"  attempts : {total_att}")
    print(f"  executed : {total_run}")
    print(f"  blocked  : {total_blk}")
    if total_fail:
        print(f"  failed   : {total_fail}")
    if total_stuck:
        print(f"  running  : {total_stuck}  ← may be stale; check for crashed processes")
    if total_att > 0:
        pct = round(total_blk / total_att * 100)
        bar = "█" * (pct // 5) + "░" * (20 - pct // 5)
        print(f"  dup rate : {pct:>3}%  [{bar}]  ({total_blk} of {total_att} were duplicates)")
    print()


def cmd_clear(store: _SQLite, path: str, wf: str | None = None, yes: bool = False) -> None:
    recs = store.all(wf)
    if not recs:
        scope = f" for workflow '{wf}'" if wf else ""
        print(f"[ledger] no records{scope}")
        return

    scope_desc = f"workflow '{wf}'" if wf else "ALL workflows"
    print(f"\n  About to delete {len(recs)} record{'s' if len(recs)!=1 else ''} from {scope_desc}:")
    print(f"  {os.path.abspath(path)}\n")

    if not yes:
        try:
            answer = input("  Type 'yes' to confirm, anything else to cancel: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print("\n[ledger] cancelled")
            return
        if answer != "yes":
            print("[ledger] cancelled")
            return

    store.clear(wf)
    print(f"[ledger] cleared {len(recs)} record{'s' if len(recs)!=1 else ''}")


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    args = sys.argv[1:]

    if not args or args[0] in ("-h", "--help"):
        print(USAGE)
        sys.exit(0)

    if args[0] in ("--version", "-V"):
        print(f"ledger {__version__}")
        sys.exit(0)

    if len(args) < 2:
        _err(f"[ledger] error: missing argument\n\n{USAGE}")
        sys.exit(1)

    cmd  = args[0]
    path = args[1]
    rest = args[2:]

    # Parse optional flags
    wf  = None
    yes = False
    i   = 0
    while i < len(rest):
        if rest[i] == "--wf" and i + 1 < len(rest):
            wf = rest[i + 1]
            i += 2
        elif rest[i] == "--yes":
            yes = True
            i += 1
        else:
            _err(f"[ledger] unknown option: {rest[i]!r}\n\n{USAGE}")
            sys.exit(1)

    if not os.path.exists(path):
        _err(f"[ledger] file not found: {path}")
        sys.exit(1)

    store = _SQLite(path)

    if cmd == "show":
        cmd_show(store, wf)
    elif cmd == "tail":
        cmd_tail(store, path, wf)
    elif cmd == "stats":
        cmd_stats(store, wf)
    elif cmd == "clear":
        cmd_clear(store, path, wf, yes)
    else:
        _err(f"[ledger] unknown command: {cmd!r}\n\n{USAGE}")
        sys.exit(1)


if __name__ == "__main__":
    main()
