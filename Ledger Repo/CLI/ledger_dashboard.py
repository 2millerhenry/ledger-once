"""
ledger_dashboard.py — Live execution control dashboard for Ledger.

Usage:
    python -m ledger_dashboard              # opens ./ledger.db on port 4242
    python -m ledger_dashboard myagent.db   # specific database
    python -m ledger_dashboard myagent.db --port 8080

Then open: http://localhost:4242

Zero dependencies beyond the standard library.
Auto-refreshes every 2 seconds.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import threading
import webbrowser
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any
from urllib.parse import urlparse, parse_qs


# ── Data layer ────────────────────────────────────────────────────────────────

def _conn(path: str) -> sqlite3.Connection:
    c = sqlite3.connect(path, check_same_thread=False)
    c.row_factory = sqlite3.Row
    return c


def _read(path: str) -> dict[str, Any]:
    if not os.path.exists(path):
        return {"records": [], "stats": _empty_stats(), "tools": [],
                "workflows": [], "callers": [], "db_exists": False}
    try:
        c   = _conn(path)
        rows = c.execute("SELECT * FROM ledger ORDER BY touched DESC").fetchall()
    except sqlite3.OperationalError:
        return {"records": [], "stats": _empty_stats(), "tools": [],
                "workflows": [], "callers": [], "db_exists": True}

    cols = [d[0] for d in c.execute("SELECT * FROM ledger LIMIT 0").description or []]

    records = []
    for r in rows:
        records.append({
            "id":       r["id"],
            "tool":     r["tool"],
            "short":    r["tool"].split(".")[-1],
            "wf":       r["wf"],
            "status":   r["status"],
            "attempts": r["attempts"],
            "runs":     r["runs"],
            "blocked":  r["blocked"],
            "error":    r["error"],
            "caller":   r["caller"] if "caller" in cols else None,
            "created":  r["created"],
            "touched":  r["touched"],
        })

    total_att  = sum(r["attempts"] for r in records)
    total_run  = sum(r["runs"]     for r in records)
    total_blk  = sum(r["blocked"]  for r in records)
    total_fail = sum(1 for r in records if r["status"] == "failed")
    dup_pct    = round(total_blk / total_att * 100) if total_att else 0

    tool_map: dict[str, dict] = {}
    for r in records:
        t = r["short"]
        if t not in tool_map:
            tool_map[t] = {"name": t, "full": r["tool"], "attempts": 0,
                           "runs": 0, "blocked": 0, "failed": 0}
        tool_map[t]["attempts"] += r["attempts"]
        tool_map[t]["runs"]     += r["runs"]
        tool_map[t]["blocked"]  += r["blocked"]
        if r["status"] == "failed":
            tool_map[t]["failed"] += 1

    tools     = sorted(tool_map.values(), key=lambda x: x["attempts"], reverse=True)
    workflows = sorted({r["wf"] for r in records})
    callers   = sorted({r["caller"] for r in records if r.get("caller")})

    total_records = len(records)
    return {
        "records":      records[:300],
        "total_records": total_records,
        "capped":       total_records > 300,
        "stats": {
            "actions":  len(records),
            "attempts": total_att,
            "executed": total_run,
            "blocked":  total_blk,
            "failed":   total_fail,
            "dup_pct":  dup_pct,
        },
        "tools":     tools,
        "workflows": workflows,
        "callers":   callers,
        "db_path":   path,
        "db_size":   _human_size(os.path.getsize(path)),
        "db_exists": True,
        "generated": datetime.now(timezone.utc).isoformat(),
    }


def _action(path: str, body: dict) -> dict:
    """Execute a control action (retry / force / clear_workflow / clear_all)."""
    if not os.path.exists(path):
        return {"ok": False, "error": "database not found"}
    try:
        c = _conn(path)
        act = body.get("action")

        if act == "retry":
            rid = body.get("id")
            if not rid:
                return {"ok": False, "error": "missing id"}
            c.execute("DELETE FROM ledger WHERE id=?", (rid,))
            c.commit()
            return {"ok": True, "msg": "Record cleared — next call will execute"}

        if act == "clear_workflow":
            wf = body.get("wf")
            if not wf:
                return {"ok": False, "error": "missing wf"}
            n = c.execute("SELECT COUNT(*) FROM ledger WHERE wf=?", (wf,)).fetchone()[0]
            c.execute("DELETE FROM ledger WHERE wf=?", (wf,))
            c.commit()
            return {"ok": True, "msg": f"Cleared {n} records for workflow '{wf}'"}

        if act == "clear_all":
            n = c.execute("SELECT COUNT(*) FROM ledger").fetchone()[0]
            c.execute("DELETE FROM ledger")
            c.commit()
            return {"ok": True, "msg": f"Cleared all {n} records"}

        return {"ok": False, "error": f"unknown action: {act!r}"}

    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def _empty_stats() -> dict:
    return {"actions": 0, "attempts": 0, "executed": 0,
            "blocked": 0, "failed": 0, "dup_pct": 0}


def _human_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


# ── HTML ──────────────────────────────────────────────────────────────────────

HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Ledger — Execution Control</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@300;400;500;600&family=IBM+Plex+Sans:wght@300;400;500&display=swap" rel="stylesheet">
<style>
  *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

  :root {
    --bg:        #0a0c0f;
    --bg2:       #0f1215;
    --bg3:       #141820;
    --bg4:       #1a2030;
    --border:    #1e2530;
    --border2:   #252d3a;
    --text:      #c8d0dc;
    --text2:     #6b7a8d;
    --text3:     #3d4a58;
    --green:     #00e676;
    --green-dim: #00291a;
    --amber:     #ffab40;
    --amber-dim: #2d1e00;
    --red:       #ff5252;
    --red-dim:   #2d0a0a;
    --blue:      #40c4ff;
    --blue-dim:  #002d3d;
    --mono:      'IBM Plex Mono', monospace;
  }

  html, body {
    background: var(--bg);
    color: var(--text);
    font-family: var(--mono);
    font-size: 13px;
    line-height: 1.6;
    min-height: 100vh;
    -webkit-font-smoothing: antialiased;
  }

  ::-webkit-scrollbar { width: 4px; height: 4px; }
  ::-webkit-scrollbar-track { background: var(--bg); }
  ::-webkit-scrollbar-thumb { background: var(--border2); border-radius: 2px; }

  .shell {
    display: grid;
    grid-template-rows: 48px 1fr;
    grid-template-columns: 210px 1fr;
    min-height: 100vh;
  }

  /* ── Topbar ── */
  .topbar {
    grid-column: 1 / -1;
    background: var(--bg2);
    border-bottom: 1px solid var(--border);
    display: flex;
    align-items: center;
    padding: 0 20px;
    gap: 16px;
    position: sticky;
    top: 0;
    z-index: 100;
  }

  .logo { font-size: 13px; font-weight: 600; letter-spacing: 0.1em; color: var(--green); text-transform: uppercase; }
  .logo span { color: var(--text3); font-weight: 300; }

  .topbar-db { font-size: 11px; color: var(--text3); border-left: 1px solid var(--border); padding-left: 16px; flex: 1; }
  .topbar-db strong { color: var(--text2); }

  .conn-indicator {
    display: flex; align-items: center; gap: 7px;
    font-size: 10px; letter-spacing: 0.08em;
    padding: 4px 10px; border-radius: 3px;
    border: 1px solid var(--border2);
    transition: all 0.3s;
  }
  .conn-indicator.live   { color: var(--green); border-color: var(--green-dim); background: var(--green-dim); }
  .conn-indicator.stale  { color: var(--amber); border-color: var(--amber-dim); background: var(--amber-dim); }
  .conn-indicator.error  { color: var(--red);   border-color: var(--red-dim);   background: var(--red-dim); }

  .conn-dot {
    width: 6px; height: 6px; border-radius: 50%;
    background: currentColor;
  }
  .conn-indicator.live .conn-dot { animation: blink 2s infinite; }
  @keyframes blink { 50% { opacity: 0.3; } }

  /* ── Sidebar ── */
  .sidebar {
    background: var(--bg2);
    border-right: 1px solid var(--border);
    padding: 16px 0;
    overflow-y: auto;
    display: flex; flex-direction: column; gap: 0;
  }

  .sidebar-section { padding: 0 12px; margin-bottom: 20px; }

  .sidebar-label {
    font-size: 9px; letter-spacing: 0.15em; text-transform: uppercase;
    color: var(--text3); margin-bottom: 6px; padding-left: 6px;
  }

  .nav-item {
    display: flex; align-items: center; gap: 8px;
    padding: 6px 10px; border-radius: 4px; cursor: pointer;
    font-size: 12px; color: var(--text2);
    transition: all 0.12s; border: 1px solid transparent; margin-bottom: 1px;
    user-select: none;
  }
  .nav-item:hover { background: var(--bg3); color: var(--text); }
  .nav-item.active { background: var(--bg3); border-color: var(--border2); color: var(--green); }
  .nav-icon { font-size: 12px; width: 14px; text-align: center; }

  /* ── Main ── */
  .main { overflow-y: auto; padding: 20px 24px; display: flex; flex-direction: column; gap: 18px; }

  /* ── Stat grid ── */
  .stat-grid { display: grid; grid-template-columns: repeat(6, 1fr); gap: 10px; }
  @media (max-width: 1100px) { .stat-grid { grid-template-columns: repeat(3, 1fr); } }

  .stat-card {
    background: var(--bg2); border: 1px solid var(--border);
    border-radius: 5px; padding: 14px 16px;
    position: relative; overflow: hidden;
  }
  .stat-card::before {
    content: ''; position: absolute; top: 0; left: 0; right: 0;
    height: 2px; background: var(--card-accent, var(--border2));
  }
  .stat-label { font-size: 9px; letter-spacing: 0.12em; text-transform: uppercase; color: var(--text3); margin-bottom: 6px; }
  .stat-value { font-size: 26px; font-weight: 600; line-height: 1; letter-spacing: -0.02em; }
  .stat-value.green { color: var(--green); }
  .stat-value.amber { color: var(--amber); }
  .stat-value.red   { color: var(--red); }
  .stat-value.blue  { color: var(--blue); }
  .stat-sub { font-size: 10px; color: var(--text3); margin-top: 5px; }

  /* ── Section header ── */
  .section-header { display: flex; align-items: center; justify-content: space-between; margin-bottom: 10px; flex-wrap: wrap; gap: 8px; }
  .section-title { font-size: 9px; letter-spacing: 0.15em; text-transform: uppercase; color: var(--text3); }
  .section-right { display: flex; align-items: center; gap: 8px; }
  .section-count { font-size: 10px; color: var(--text3); background: var(--bg3); border: 1px solid var(--border); padding: 2px 8px; border-radius: 3px; }

  /* ── Table ── */
  .table-wrap { background: var(--bg2); border: 1px solid var(--border); border-radius: 5px; overflow: hidden; }

  table { width: 100%; border-collapse: collapse; }
  thead th {
    background: var(--bg3); padding: 9px 12px; text-align: left;
    font-size: 9px; letter-spacing: 0.12em; text-transform: uppercase;
    color: var(--text3); border-bottom: 1px solid var(--border); font-weight: 400; white-space: nowrap;
  }
  tbody tr { border-bottom: 1px solid var(--border); transition: background 0.1s; }
  tbody tr:last-child { border-bottom: none; }
  tbody tr:hover { background: var(--bg3); }
  tbody td { padding: 9px 12px; font-size: 12px; color: var(--text); vertical-align: middle; }

  .td-tool { font-weight: 500; }
  .td-full { font-size: 10px; color: var(--text3); margin-top: 1px; }
  .td-error { font-size: 10px; color: var(--red); margin-top: 2px; }
  .td-mono { font-family: var(--mono); font-size: 11px; }

  /* ── Badges ── */
  .badge {
    display: inline-flex; align-items: center; gap: 4px;
    padding: 2px 7px; border-radius: 3px;
    font-size: 10px; font-weight: 500; letter-spacing: 0.05em; text-transform: uppercase; white-space: nowrap;
  }
  .badge::before { content: ''; width: 4px; height: 4px; border-radius: 50%; background: currentColor; }
  .badge-success { background: var(--green-dim); color: var(--green); }
  .badge-failed  { background: var(--red-dim);   color: var(--red); }
  .badge-running { background: var(--blue-dim);  color: var(--blue); animation: badge-blink 1.5s infinite; }
  @keyframes badge-blink { 50% { opacity: 0.5; } }

  /* ── Blocked bar ── */
  .blocked-bar { display: flex; align-items: center; gap: 6px; }
  .bar-track { flex: 1; height: 3px; background: var(--bg3); border-radius: 2px; overflow: hidden; max-width: 72px; }
  .bar-fill { height: 100%; border-radius: 2px; background: var(--amber); transition: width 0.3s; }
  .bar-fill.danger { background: var(--red); }
  .bar-pct { font-size: 10px; color: var(--text3); min-width: 24px; text-align: right; }

  /* ── Live feed ── */
  .feed { background: var(--bg2); border: 1px solid var(--border); border-radius: 5px; max-height: 300px; overflow-y: auto; }
  .feed-row {
    display: grid; grid-template-columns: 68px 14px 1fr auto 72px 32px;
    align-items: center; gap: 10px; padding: 7px 12px;
    border-bottom: 1px solid var(--border); font-size: 11px;
    animation: row-in 0.25s ease;
  }
  @keyframes row-in { from { opacity: 0; transform: translateX(-4px); } to { opacity: 1; transform: none; } }
  .feed-row:last-child { border-bottom: none; }
  .feed-row:hover { background: var(--bg3); }
  .feed-ts { color: var(--text3); font-size: 10px; }
  .feed-ok  { color: var(--green); }
  .feed-err { color: var(--red); }
  .feed-run { color: var(--blue); }
  .feed-tool { font-weight: 500; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .feed-wf { color: var(--text3); font-size: 10px; text-align: right; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .feed-blk { color: var(--amber); font-size: 10px; text-align: right; white-space: nowrap; }

  /* ── Tool cards ── */
  .tool-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(260px, 1fr)); gap: 10px; }
  .tool-card { background: var(--bg2); border: 1px solid var(--border); border-radius: 5px; padding: 14px; transition: border-color 0.15s; }
  .tool-card:hover { border-color: var(--border2); }
  .tool-card-name { font-size: 13px; font-weight: 600; margin-bottom: 3px; }
  .tool-card-full { font-size: 10px; color: var(--text3); margin-bottom: 10px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .tool-card-row { display: flex; justify-content: space-between; font-size: 11px; color: var(--text2); margin-bottom: 3px; }
  .tool-card-row span:last-child { color: var(--text); font-weight: 500; }
  .tool-dup-bar { margin-top: 8px; height: 2px; background: var(--bg3); border-radius: 2px; overflow: hidden; }
  .tool-dup-fill { height: 100%; background: var(--amber); border-radius: 2px; transition: width 0.5s; }
  .tool-dup-fill.high { background: var(--red); }

  /* ── Alerts ── */
  .alert-banner {
    background: var(--red-dim); border: 1px solid rgba(255,82,82,0.3);
    border-radius: 5px; padding: 10px 14px;
    display: flex; align-items: center; gap: 8px;
    font-size: 12px; color: var(--red); margin-bottom: 4px;
  }

  /* ── Control buttons ── */
  .btn {
    display: inline-flex; align-items: center; gap: 5px;
    padding: 5px 12px; border-radius: 4px; cursor: pointer;
    font-family: var(--mono); font-size: 11px; font-weight: 500;
    border: 1px solid; transition: all 0.15s; white-space: nowrap;
    user-select: none;
  }
  .btn:active { transform: scale(0.97); }

  .btn-retry  { color: var(--amber); border-color: rgba(255,171,64,0.3); background: var(--amber-dim); }
  .btn-retry:hover  { border-color: var(--amber); }

  .btn-danger { color: var(--red); border-color: rgba(255,82,82,0.3); background: var(--red-dim); }
  .btn-danger:hover { border-color: var(--red); }

  .btn-neutral { color: var(--text2); border-color: var(--border2); background: var(--bg3); }
  .btn-neutral:hover { color: var(--text); border-color: var(--text3); }

  /* ── Toast ── */
  #toast {
    position: fixed; bottom: 24px; left: 50%; transform: translateX(-50%);
    background: var(--bg4); border: 1px solid var(--border2); border-radius: 6px;
    padding: 10px 20px; font-size: 12px; color: var(--text);
    z-index: 999; pointer-events: none;
    opacity: 0; transition: opacity 0.2s;
    box-shadow: 0 8px 32px rgba(0,0,0,0.5);
  }
  #toast.show { opacity: 1; }
  #toast.ok  { color: var(--green); border-color: var(--green-dim); }
  #toast.err { color: var(--red);   border-color: var(--red-dim); }

  /* ── Filters ── */
  .filter-bar { display: flex; gap: 6px; flex-wrap: wrap; }
  .filter-select {
    background: var(--bg2); border: 1px solid var(--border2); color: var(--text2);
    font-family: var(--mono); font-size: 11px; padding: 4px 9px;
    border-radius: 4px; cursor: pointer; outline: none; transition: border-color 0.15s;
  }
  .filter-select:hover, .filter-select:focus { border-color: var(--green); color: var(--text); }

  /* ── Empty ── */
  .empty { padding: 48px 20px; text-align: center; color: var(--text3); }
  .empty-icon { font-size: 28px; margin-bottom: 10px; }
  .empty-title { font-size: 13px; color: var(--text2); margin-bottom: 6px; }
  .empty-code {
    display: inline-block; background: var(--bg3); border: 1px solid var(--border2);
    border-radius: 3px; padding: 1px 7px; color: var(--green); font-size: 11px; margin: 1px;
  }

  /* ── Confirm modal ── */
  #confirm-modal {
    display: none; position: fixed; inset: 0;
    background: rgba(10,12,15,0.85); z-index: 500;
    align-items: center; justify-content: center;
  }
  #confirm-modal.show { display: flex; }
  .modal-box {
    background: var(--bg2); border: 1px solid var(--border2);
    border-radius: 6px; padding: 28px 32px; max-width: 400px; width: 90%;
    text-align: center;
  }
  .modal-title { font-size: 14px; font-weight: 600; margin-bottom: 8px; }
  .modal-sub { font-size: 12px; color: var(--text2); margin-bottom: 20px; line-height: 1.7; }
  .modal-btns { display: flex; gap: 8px; justify-content: center; }

  /* ── No-db overlay ── */
  #no-db {
    display: none; position: fixed; inset: 0;
    background: rgba(10,12,15,0.92); z-index: 200;
    align-items: center; justify-content: center;
  }
  #no-db.show { display: flex; }
  .no-db-box {
    background: var(--bg2); border: 1px solid var(--border2); border-radius: 6px;
    padding: 36px 44px; text-align: center; max-width: 460px;
  }
  .no-db-title { font-size: 15px; font-weight: 600; margin-bottom: 10px; }
  .no-db-sub { font-size: 12px; color: var(--text2); line-height: 2; }

  .view { display: none; }
  .view.active { display: contents; }
</style>
</head>
<body>

<!-- No-records overlay -->
<div id="no-db">
  <div class="no-db-box">
    <div style="font-size:28px;margin-bottom:14px;color:var(--text3)">⬡</div>
    <div class="no-db-title">Waiting for first action</div>
    <div class="no-db-sub">
      Start your agent with:<br>
      <span class="empty-code">from ledger import guard</span><br>
      <span class="empty-code">guard(your_tool, ...)</span><br><br>
      This dashboard updates automatically every 2 seconds.
    </div>
  </div>
</div>

<!-- Confirm modal -->
<div id="confirm-modal">
  <div class="modal-box">
    <div class="modal-title" id="modal-title">Are you sure?</div>
    <div class="modal-sub" id="modal-sub"></div>
    <div class="modal-btns">
      <button class="btn btn-neutral" onclick="closeModal()">Cancel</button>
      <button class="btn btn-danger" id="modal-confirm-btn" onclick="confirmAction()">Confirm</button>
    </div>
  </div>
</div>

<!-- Toast -->
<div id="toast"></div>

<div class="shell">

  <header class="topbar">
    <div class="logo">LEDGER <span>/ control</span></div>
    <div class="topbar-db">
      <strong id="db-path">—</strong>&nbsp;·&nbsp;<span id="db-size">—</span>
    </div>
    <div class="conn-indicator live" id="conn-indicator">
      <div class="conn-dot"></div>
      <span id="conn-label">live</span>
    </div>
  </header>

  <nav class="sidebar">
    <div class="sidebar-section">
      <div class="sidebar-label">Views</div>
      <div class="nav-item active" onclick="setView('overview')" id="nav-overview">
        <span class="nav-icon">◈</span> Overview
      </div>
      <div class="nav-item" onclick="setView('feed')" id="nav-feed">
        <span class="nav-icon">◎</span> Live Feed
      </div>
      <div class="nav-item" onclick="setView('tools')" id="nav-tools">
        <span class="nav-icon">◇</span> Tools
      </div>
      <div class="nav-item" onclick="setView('history')" id="nav-history">
        <span class="nav-icon">≡</span> History
      </div>
    </div>

    <div class="sidebar-section">
      <div class="sidebar-label">Workflows</div>
      <div id="wf-nav"></div>
    </div>

    <div class="sidebar-section" id="callers-section" style="display:none">
      <div class="sidebar-label">Callers</div>
      <div id="caller-nav"></div>
    </div>

    <div class="sidebar-section" style="margin-top:auto; padding-top:16px; border-top:1px solid var(--border)">
      <div class="sidebar-label">Danger zone</div>
      <div class="nav-item" onclick="askClearAll()" style="color:var(--red)">
        <span class="nav-icon">✕</span> Clear all records
      </div>
    </div>
  </nav>

  <main class="main" id="main-content">

    <!-- OVERVIEW -->
    <div id="view-overview" class="view active">
      <div id="alert-zone"></div>
      <div class="stat-grid" id="stat-grid"></div>
      <div>
        <div class="section-header">
          <span class="section-title">Recent Activity</span>
          <span class="section-count" id="feed-count">—</span>
        </div>
        <div class="feed" id="feed-overview"></div>
      </div>
      <div>
        <div class="section-header">
          <span class="section-title">Tool Health</span>
        </div>
        <div class="tool-grid" id="tool-overview"></div>
      </div>
    </div>

    <!-- LIVE FEED -->
    <div id="view-feed" class="view">
      <div>
        <div class="section-header">
          <span class="section-title">Live Feed</span>
          <div class="filter-bar">
            <select class="filter-select" id="feed-filter-status" onchange="renderFeed()">
              <option value="">All statuses</option>
              <option value="success">Success</option>
              <option value="failed">Failed</option>
              <option value="running">Running</option>
            </select>
            <select class="filter-select" id="feed-filter-wf" onchange="renderFeed()">
              <option value="">All workflows</option>
            </select>
          </div>
        </div>
        <div class="table-wrap">
          <table>
            <thead><tr>
              <th>Time</th><th>Status</th><th>Tool</th>
              <th>Workflow</th><th>Caller</th>
              <th style="text-align:right">Attempts</th>
              <th style="text-align:right">Blocked</th>
              <th></th>
            </tr></thead>
            <tbody id="feed-table-body"></tbody>
          </table>
        </div>
      </div>
    </div>

    <!-- TOOLS -->
    <div id="view-tools" class="view">
      <div>
        <div class="section-header">
          <span class="section-title">Tool Breakdown</span>
          <span class="section-count" id="tools-count">—</span>
        </div>
        <div class="tool-grid" id="tool-detail-grid"></div>
      </div>
    </div>

    <!-- HISTORY -->
    <div id="view-history" class="view">
      <div>
        <div class="section-header">
          <span class="section-title">History</span>
          <div class="section-right">
            <div class="filter-bar">
              <select class="filter-select" id="hist-filter-status" onchange="renderHistory()">
                <option value="">All statuses</option>
                <option value="success">Success</option>
                <option value="failed">Failed</option>
                <option value="running">Running</option>
              </select>
              <select class="filter-select" id="hist-filter-wf" onchange="renderHistory()">
                <option value="">All workflows</option>
              </select>
              <select class="filter-select" id="hist-filter-tool" onchange="renderHistory()">
                <option value="">All tools</option>
              </select>
            </div>
            <button class="btn btn-danger" onclick="askClearFiltered()">Clear filtered</button>
          </div>
        </div>
        <div class="table-wrap">
          <table>
            <thead><tr>
              <th>Tool</th><th>Status</th><th>Workflow</th>
              <th>Caller</th>
              <th style="text-align:right">Attempts</th>
              <th style="text-align:right">Executed</th>
              <th>Blocked</th>
              <th>Last seen</th>
              <th></th>
            </tr></thead>
            <tbody id="hist-table-body"></tbody>
          </table>
        </div>
      </div>
    </div>

  </main>
</div>

<script>
// ── State ──────────────────────────────────────────────────────────────────
let _data        = null;
let _view        = 'overview';
let _wfFilter    = '';
let _pendingAct  = null;
let _missedPolls = 0;
let _hasSeenRecords = false;

// ── Polling ────────────────────────────────────────────────────────────────
async function refresh() {
  try {
    const r = await fetch('/api/data');
    if (!r.ok) throw new Error(r.status);
    _data        = await r.json();
    _missedPolls = 0;
    setConn('live', 'live');
    render();
    if (_data.records.length > 0) {
      document.getElementById('no-db').classList.remove('show');
      _hasSeenRecords = true;
    } else if (!_hasSeenRecords) {
      document.getElementById('no-db').classList.add('show');
    } else {
      // Records were cleared — show empty state in-UI, not the blocking overlay
      document.getElementById('no-db').classList.remove('show');
    }
  } catch(e) {
    _missedPolls++;
    if (_missedPolls === 2)  setConn('stale', 'slow…');
    if (_missedPolls >= 4)   setConn('error', 'no connection');
  }
}

setInterval(refresh, 2000);
refresh();

function setConn(cls, label) {
  const el = document.getElementById('conn-indicator');
  el.className = 'conn-indicator ' + cls;
  document.getElementById('conn-label').textContent = label;
}

// ── View switching ─────────────────────────────────────────────────────────
function setView(v) {
  _view = v;
  document.querySelectorAll('.view').forEach(e => e.classList.remove('active'));
  document.querySelectorAll('.nav-item').forEach(e => e.classList.remove('active'));
  document.getElementById('view-' + v).classList.add('active');
  document.getElementById('nav-' + v).classList.add('active');
  render();
}

// ── Render ─────────────────────────────────────────────────────────────────
function render() {
  if (!_data) return;
  const d = _data;

  document.getElementById('db-path').textContent = d.db_path || '—';
  document.getElementById('db-size').textContent = d.db_size || '—';

  // Sidebar workflows
  const wfNav = document.getElementById('wf-nav');
  wfNav.innerHTML = (d.workflows || []).map(wf =>
    `<div class="nav-item ${_wfFilter===wf?'active':''}" onclick="setWf('${esc(wf)}')" style="font-size:11px">
      <span class="nav-icon" style="font-size:9px">◦</span>${esc(wf)}
      <button class="btn btn-danger" style="margin-left:auto;padding:1px 6px;font-size:9px"
              onclick="event.stopPropagation();askClearWf('${esc(wf)}')">✕</button>
    </div>`
  ).join('') || `<div style="padding:4px 10px;font-size:10px;color:var(--text3)">none</div>`;

  // Sidebar callers
  const callers = d.callers || [];
  document.getElementById('callers-section').style.display = callers.length ? '' : 'none';
  document.getElementById('caller-nav').innerHTML = callers.map(c =>
    `<div class="nav-item" style="font-size:11px">
      <span class="nav-icon" style="font-size:9px">◈</span>${esc(c)}
    </div>`
  ).join('');

  if (_view === 'overview') renderOverview();
  if (_view === 'feed')     renderFeed();
  if (_view === 'tools')    renderTools();
  if (_view === 'history')  renderHistory();
}

function setWf(wf) {
  _wfFilter = _wfFilter === wf ? '' : wf;
  setView('history');
}

// ── Overview ───────────────────────────────────────────────────────────────
function renderOverview() {
  const s = _data.stats;

  // Alerts
  const alerts = [];
  if (s.failed > 0)
    alerts.push(`${s.failed} tool call${s.failed>1?'s':''} failed — check the History tab`);
  if (s.dup_pct > 50)
    alerts.push(`${s.dup_pct}% duplicate rate — your agent may be stuck in a retry loop`);
  if (d.capped)
    alerts.push(`Showing 300 of ${d.total_records} records — use the CLI for full history: python ledger_cli.py show ledger.db`);
  document.getElementById('alert-zone').innerHTML =
    alerts.map(a => `<div class="alert-banner">⚠ ${a}</div>`).join('');

  document.getElementById('stat-grid').innerHTML = `
    <div class="stat-card" style="--card-accent:var(--blue)">
      <div class="stat-label">Actions</div>
      <div class="stat-value blue">${s.actions}</div>
      <div class="stat-sub">unique tool calls</div>
    </div>
    <div class="stat-card">
      <div class="stat-label">Attempts</div>
      <div class="stat-value">${s.attempts}</div>
      <div class="stat-sub">inc. duplicates</div>
    </div>
    <div class="stat-card" style="--card-accent:var(--green)">
      <div class="stat-label">Executed</div>
      <div class="stat-value green">${s.executed}</div>
      <div class="stat-sub">actually ran</div>
    </div>
    <div class="stat-card" style="--card-accent:${s.blocked>0?'var(--amber)':''}">
      <div class="stat-label">Blocked</div>
      <div class="stat-value ${s.blocked>0?'amber':''}">${s.blocked}</div>
      <div class="stat-sub">duplicates stopped</div>
    </div>
    <div class="stat-card" style="--card-accent:${s.dup_pct>30?'var(--amber)':''}">
      <div class="stat-label">Dup Rate</div>
      <div class="stat-value ${s.dup_pct>50?'red':s.dup_pct>20?'amber':''}">${s.dup_pct}%</div>
      <div class="stat-sub">of all attempts</div>
    </div>
    <div class="stat-card" style="--card-accent:${s.failed>0?'var(--red)':''}">
      <div class="stat-label">Failed</div>
      <div class="stat-value ${s.failed>0?'red':''}">${s.failed}</div>
      <div class="stat-sub">tool errors</div>
    </div>
  `;

  document.getElementById('feed-count').textContent =
    `${_data.records.length} action${_data.records.length!==1?'s':''}`;

  const feedEl = document.getElementById('feed-overview');
  const rows   = _data.records.slice(0, 20);
  feedEl.innerHTML = rows.length
    ? rows.map(r => feedRow(r)).join('')
    : emptyState('No actions yet');

  document.getElementById('tool-overview').innerHTML =
    _data.tools.map(t => toolCard(t)).join('');
}

// ── Feed ───────────────────────────────────────────────────────────────────
function renderFeed() {
  if (!_data) return;
  populateSelect('feed-filter-wf', _data.workflows, 'All workflows');

  const sf = val('feed-filter-status');
  const wf = val('feed-filter-wf');

  let rows = _data.records;
  if (sf) rows = rows.filter(r => r.status === sf);
  if (wf) rows = rows.filter(r => r.wf === wf);

  const tbody = document.getElementById('feed-table-body');
  tbody.innerHTML = rows.length
    ? rows.map(r => `
        <tr>
          <td class="td-mono" style="color:var(--text3);font-size:10px" title="${esc(r.touched)}">${fmtTime(r.touched)}</td>
          <td>${badge(r.status)}</td>
          <td>
            <div class="td-tool">${esc(r.short)}</div>
            <div class="td-full">${esc(r.tool)}</div>
            ${r.error ? `<div class="td-error">${esc(r.error.slice(0,80))}</div>` : ''}
          </td>
          <td style="font-size:11px;color:var(--text2)">${esc(r.wf)}</td>
          <td style="font-size:10px;color:var(--text3)">${esc(r.caller||'—')}</td>
          <td class="td-mono" style="text-align:right">${r.attempts}</td>
          <td class="td-mono" style="text-align:right;color:${r.blocked>0?'var(--amber)':'var(--text3)'}">${r.blocked||'—'}</td>
          <td><button class="btn btn-retry" onclick="retryRecord('${esc(r.id)}','${esc(r.short)}')">↺ retry</button></td>
        </tr>`).join('')
    : `<tr><td colspan="8">${emptyState('No matching records')}</td></tr>`;
}

// ── Tools ──────────────────────────────────────────────────────────────────
function renderTools() {
  if (!_data) return;
  document.getElementById('tools-count').textContent =
    `${_data.tools.length} tool${_data.tools.length!==1?'s':''}`;
  document.getElementById('tool-detail-grid').innerHTML =
    _data.tools.length
      ? _data.tools.map(t => toolCard(t)).join('')
      : emptyState('No tools recorded yet');
}

// ── History ────────────────────────────────────────────────────────────────
function renderHistory() {
  if (!_data) return;
  populateSelect('hist-filter-wf',   _data.workflows,                'All workflows');
  populateSelect('hist-filter-tool', _data.tools.map(t => t.name),   'All tools');

  const sf = val('hist-filter-status');
  const wf = _wfFilter || val('hist-filter-wf');
  const tf = val('hist-filter-tool');

  let rows = _data.records;
  if (sf) rows = rows.filter(r => r.status === sf);
  if (wf) rows = rows.filter(r => r.wf === wf);
  if (tf) rows = rows.filter(r => r.short === tf);

  const tbody = document.getElementById('hist-table-body');
  tbody.innerHTML = rows.length
    ? rows.map(r => {
        const pct = r.attempts > 0 ? Math.round(r.blocked/r.attempts*100) : 0;
        return `<tr>
          <td>
            <div class="td-tool">${esc(r.short)}</div>
            <div class="td-full">${esc(r.tool)}</div>
            ${r.error ? `<div class="td-error">${esc(r.error.slice(0,72))}</div>` : ''}
          </td>
          <td>${badge(r.status)}</td>
          <td style="font-size:11px;color:var(--text2)">${esc(r.wf)}</td>
          <td style="font-size:10px;color:var(--text3)">${esc(r.caller||'—')}</td>
          <td class="td-mono" style="text-align:right">${r.attempts}</td>
          <td class="td-mono" style="text-align:right;color:var(--green)">${r.runs}</td>
          <td>
            <div class="blocked-bar">
              <div class="bar-track"><div class="bar-fill ${pct>50?'danger':''}" style="width:${pct}%"></div></div>
              <span class="bar-pct" style="color:${r.blocked>0?'var(--amber)':'var(--text3)'}">${r.blocked||'—'}</span>
            </div>
          </td>
          <td class="td-mono" style="font-size:10px;color:var(--text3)" title="${esc(r.touched)}">${fmtTime(r.touched)}</td>
          <td><button class="btn btn-retry" onclick="retryRecord('${esc(r.id)}','${esc(r.short)}')">↺ retry</button></td>
        </tr>`;
      }).join('')
    : `<tr><td colspan="9">${emptyState('No matching records')}</td></tr>`;
}

// ── Components ─────────────────────────────────────────────────────────────
function feedRow(r) {
  const icon = r.status==='success' ? '✓' : r.status==='failed' ? '✗' : '⟳';
  const cls  = r.status==='success' ? 'feed-ok' : r.status==='failed' ? 'feed-err' : 'feed-run';
  return `<div class="feed-row">
    <span class="feed-ts" title="${esc(r.touched)}">${fmtTime(r.touched)}</span>
    <span class="${cls}">${icon}</span>
    <span class="feed-tool">${esc(r.short)}</span>
    <span class="feed-wf">${esc(r.wf)}</span>
    <span class="feed-blk">${r.blocked>0?`+${r.blocked} blk`:''}</span>
    <button class="btn btn-retry" style="padding:2px 8px;font-size:10px" onclick="retryRecord('${esc(r.id)}','${esc(r.short)}')">↺</button>
  </div>`;
}

function toolCard(t) {
  const dup  = t.attempts > 0 ? Math.round(t.blocked/t.attempts*100) : 0;
  const high = dup > 50;
  return `<div class="tool-card">
    <div class="tool-card-name">${esc(t.name)}</div>
    <div class="tool-card-full">${esc(t.full)}</div>
    <div class="tool-card-row"><span>Attempts</span><span>${t.attempts}</span></div>
    <div class="tool-card-row"><span>Executed</span><span style="color:var(--green)">${t.runs}</span></div>
    <div class="tool-card-row"><span>Blocked</span><span style="color:${t.blocked>0?'var(--amber)':'var(--text3)'}">${t.blocked}</span></div>
    ${t.failed>0?`<div class="tool-card-row"><span>Failed</span><span style="color:var(--red)">${t.failed}</span></div>`:''}
    <div class="tool-dup-bar"><div class="tool-dup-fill ${high?'high':''}" style="width:${dup}%"></div></div>
    ${dup>0?`<div style="font-size:10px;color:${high?'var(--red)':'var(--amber)'};margin-top:4px">${dup}% dup rate</div>`:''}
  </div>`;
}

function badge(s) {
  if (s==='success') return '<span class="badge badge-success">success</span>';
  if (s==='failed')  return '<span class="badge badge-failed">failed</span>';
  return '<span class="badge badge-running">running</span>';
}

function emptyState(msg) {
  return `<div class="empty"><div class="empty-icon">◌</div><div class="empty-title">${msg}</div></div>`;
}

// ── Control actions ────────────────────────────────────────────────────────
async function doAction(body) {
  try {
    const r   = await fetch('/api/action', { method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(body) });
    const res = await r.json();
    toast(res.ok, res.msg || res.error || '?');
    if (res.ok) await refresh();
  } catch(e) {
    toast(false, 'Request failed: ' + e.message);
  }
}

function retryRecord(id, name) {
  showModal(
    `Retry "${name}"?`,
    'This clears the record so the next call to this tool will execute again.',
    () => doAction({ action: 'retry', id })
  );
}

function askClearWf(wf) {
  showModal(
    `Clear workflow "${wf}"?`,
    `All ${_data.records.filter(r=>r.wf===wf).length} records for this workflow will be deleted. Tools can execute again for this workflow.`,
    () => doAction({ action: 'clear_workflow', wf })
  );
}

function askClearAll() {
  showModal(
    'Clear all records?',
    `All ${_data.stats.actions} records will be permanently deleted. Every tool can execute again.`,
    () => doAction({ action: 'clear_all' })
  );
}

function askClearFiltered() {
  const wf = _wfFilter || val('hist-filter-wf');
  if (wf) { askClearWf(wf); return; }
  askClearAll();
}

// ── Modal ──────────────────────────────────────────────────────────────────
function showModal(title, sub, onConfirm) {
  _pendingAct = onConfirm;
  document.getElementById('modal-title').textContent = title;
  document.getElementById('modal-sub').textContent   = sub;
  document.getElementById('confirm-modal').classList.add('show');
}

function closeModal() {
  _pendingAct = null;
  document.getElementById('confirm-modal').classList.remove('show');
}

function confirmAction() {
  closeModal();
  if (_pendingAct) _pendingAct();
}

document.getElementById('confirm-modal').addEventListener('click', e => {
  if (e.target === document.getElementById('confirm-modal')) closeModal();
});

// ── Toast ──────────────────────────────────────────────────────────────────
let _toastTimer = null;
function toast(ok, msg) {
  const el = document.getElementById('toast');
  el.textContent  = (ok ? '✓ ' : '✗ ') + msg;
  el.className    = 'show ' + (ok ? 'ok' : 'err');
  clearTimeout(_toastTimer);
  _toastTimer = setTimeout(() => el.classList.remove('show'), 3000);
}

// ── Helpers ────────────────────────────────────────────────────────────────
function esc(s) {
  return String(s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

function fmtTime(iso) {
  if (!iso) return '—';
  try {
    const d   = new Date(iso);
    const now = new Date();
    const sameDay = d.toDateString() === now.toDateString();
    return sameDay
      ? d.toLocaleTimeString()
      : d.toLocaleDateString(undefined,{month:'short',day:'numeric'}) + ' ' + d.toLocaleTimeString();
  } catch { return iso; }
}

function val(id) {
  return document.getElementById(id)?.value || '';
}

function populateSelect(id, items, placeholder) {
  const el = document.getElementById(id);
  if (!el) return;
  const cur = el.value;
  el.innerHTML = `<option value="">${placeholder}</option>` +
    items.map(v => `<option value="${esc(v)}" ${v===cur?'selected':''}>${esc(v)}</option>`).join('');
  if (_wfFilter && id.includes('wf')) el.value = _wfFilter;
}
</script>
</body>
</html>"""


# ── HTTP server ───────────────────────────────────────────────────────────────

class _Handler(BaseHTTPRequestHandler):
    db_path: str = "ledger.db"

    def do_GET(self) -> None:   # noqa: N802
        path = urlparse(self.path).path
        if path == "/api/data":
            payload = json.dumps(_read(self.db_path), default=str).encode()
            self._respond(200, "application/json", payload)
        elif path in ("/", "/index.html"):
            self._respond(200, "text/html; charset=utf-8", HTML.encode())
        else:
            self._respond(404, "text/plain", b"not found")

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path == "/api/action":
            length = int(self.headers.get("Content-Length", 0))
            body   = json.loads(self.rfile.read(length) or b"{}")
            result = _action(self.db_path, body)
            payload = json.dumps(result).encode()
            self._respond(200, "application/json", payload)
        else:
            self._respond(404, "text/plain", b"not found")

    def _respond(self, code: int, content_type: str, body: bytes) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt: str, *args: Any) -> None:
        pass  # silence access log


# ── Entry point ───────────────────────────────────────────────────────────────

def serve(db_path: str = "ledger.db", port: int = 4242, open_browser: bool = True) -> None:
    _Handler.db_path = db_path
    server = HTTPServer(("127.0.0.1", port), _Handler)
    url    = f"http://localhost:{port}"

    print(f"\n  ⬡  Ledger Dashboard")
    print(f"     {url}")
    print(f"     database : {os.path.abspath(db_path)}")
    print(f"     ⚠  localhost only — do not expose this port on a public network")
    print(f"\n  Ctrl-C to stop\n")

    if open_browser:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[ledger] dashboard stopped")
        server.server_close()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Ledger — live execution control dashboard",
        epilog="  Example: python -m ledger_dashboard myagent.db --port 8080",
    )
    parser.add_argument(
        "db", nargs="?", default=os.environ.get("LEDGER_DB", "ledger.db"),
        help="path to ledger.db  (default: ./ledger.db or $LEDGER_DB)",
    )
    parser.add_argument("--port", "-p", type=int, default=4242)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()
    serve(args.db, args.port, open_browser=not args.no_browser)


if __name__ == "__main__":
    main()
