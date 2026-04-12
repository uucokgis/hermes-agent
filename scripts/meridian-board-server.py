#!/usr/bin/env python3
"""
Meridian Board — Jira-style kanban dashboard server.
Reads task files from the workspace and serves a web UI.

Usage:
    python3 meridian-board-server.py [--port 8765] [--workspace /home/umut/meridian]
"""

import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import urlparse, unquote

# ---------------------------------------------------------------------------
# Task parsing
# ---------------------------------------------------------------------------

QUEUE_ORDER = ["backlog", "ready", "in_progress", "review", "waiting_human", "done", "debt"]
QUEUE_ALIASES = {"in_progress": ["in-progress"], "in-progress": ["in_progress"]}
QUEUE_LABELS = {
    "backlog": "Backlog", "ready": "Ready", "in_progress": "In Progress",
    "review": "Review", "waiting_human": "Waiting Human", "done": "Done", "debt": "Debt",
}


def parse_frontmatter(text: str) -> tuple[dict, str]:
    """Parse YAML frontmatter. Returns (meta dict, body text)."""
    meta = {}
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        # No frontmatter — look for H1
        for line in lines:
            if line.startswith("# "):
                meta["title"] = line[2:].strip()
                break
        return meta, text

    end = None
    for i, line in enumerate(lines[1:], 1):
        if line.strip() == "---":
            end = i
            break

    if end is None:
        return meta, text

    for line in lines[1:end]:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if ":" in stripped:
            key, _, val = stripped.partition(":")
            val = val.strip().strip('"').strip("'")
            if val and not val.startswith("[") and not val.startswith("- "):
                meta[key.strip()] = val

    body = "\n".join(lines[end + 1:]).strip()
    # Fallback title from H1 in body
    if not meta.get("title"):
        for line in body.splitlines():
            s = line.strip()
            if s.startswith("# "):
                meta["title"] = s[2:].strip()
                break
            if s.startswith("## Task ID:"):
                meta.setdefault("id", s.split(":", 1)[1].strip())
            if s.startswith("## Status:"):
                meta.setdefault("status", s.split(":", 1)[1].strip())

    return meta, body


def humanize_stem(stem: str) -> str:
    """Turn a filename stem into a readable title."""
    s = re.sub(r'-?(READY|IN[_-]PROGRESS|CLAIM|REWORK|REQUEST[_-]CHANGES|REVIEW|IN.PROGRESS)[^-]*$', '', stem, flags=re.IGNORECASE)
    s = re.sub(r'[-_]?20\d{6,8}', '', s)
    s = re.sub(r'^(PHILIP|FATIH|MATTHEW)[_-]?(\d{3})?[_-]?', '', s, flags=re.IGNORECASE)
    s = s.replace('-', ' ').replace('_', ' ').strip()
    return s if len(s) > 3 else stem


def read_task(path: Path, queue: str) -> dict:
    try:
        text = path.read_text(errors="replace")
    except OSError:
        text = ""

    meta, body = parse_frontmatter(text)
    task_id = meta.get("id") or path.stem
    title = meta.get("title") or humanize_stem(path.stem)

    assignee = (meta.get("claimed_by") or meta.get("assigned_to") or meta.get("assignee") or "")
    reviewer = meta.get("reviewer") or ""
    priority = meta.get("priority") or "medium"
    component = meta.get("component") or meta.get("type") or ""
    created_at = meta.get("created_at") or meta.get("date") or ""
    updated_at = meta.get("updated_at") or ""

    agent = ""
    fname = path.name.upper()
    for a in ("PHILIP", "FATIH", "MATTHEW"):
        if a in fname or a in task_id.upper() or a.lower() == assignee.lower():
            agent = a.lower()
            break

    try:
        mtime = path.stat().st_mtime
        age_hours = (datetime.now(timezone.utc).timestamp() - mtime) / 3600
        if age_hours < 1:
            age = f"{int(age_hours * 60)}m ago"
        elif age_hours < 24:
            age = f"{int(age_hours)}h ago"
        else:
            age = f"{int(age_hours / 24)}d ago"
        mtime_iso = datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M")
    except OSError:
        age = ""
        mtime_iso = ""

    # Count checklist items
    done_items = len(re.findall(r'^\s*-\s*\[x\]', body, re.MULTILINE | re.IGNORECASE))
    todo_items = len(re.findall(r'^\s*-\s*\[ \]', body, re.MULTILINE))

    return {
        "id": task_id,
        "filename": path.name,
        "queue": queue,
        "title": title,
        "assignee": assignee,
        "reviewer": reviewer,
        "agent": agent,
        "priority": priority,
        "component": component,
        "created_at": created_at,
        "updated_at": updated_at,
        "mtime": mtime_iso,
        "age": age,
        "done_items": done_items,
        "todo_items": todo_items,
    }


def read_task_detail(path: Path, queue: str) -> dict:
    """Full task data including body for the detail panel."""
    task = read_task(path, queue)
    try:
        raw = path.read_text(errors="replace")
    except OSError:
        raw = ""
    meta, body = parse_frontmatter(raw)
    task["body"] = body
    task["raw"] = raw
    return task


def load_board(workspace: Path) -> dict:
    tasks_root = workspace / "tasks"
    board = {q: [] for q in QUEUE_ORDER}

    for queue in QUEUE_ORDER:
        dirs = [tasks_root / queue]
        for alias in QUEUE_ALIASES.get(queue, []):
            dirs.append(tasks_root / alias)

        seen: set[str] = set()
        for d in dirs:
            if not d.is_dir():
                continue
            for path in sorted(d.iterdir()):
                if not path.is_file() or path.name.startswith("."):
                    continue
                if path.name in seen:
                    continue
                seen.add(path.name)
                board[queue].append(read_task(path, queue))

    return board


def find_task_path(workspace: Path, filename: str) -> Path | None:
    tasks_root = workspace / "tasks"
    for queue in QUEUE_ORDER:
        for subdir in [queue] + QUEUE_ALIASES.get(queue, []):
            p = tasks_root / subdir / filename
            if p.is_file():
                return p
    return None


# ---------------------------------------------------------------------------
# Markdown → HTML (lightweight, no deps)
# ---------------------------------------------------------------------------

def md_to_html(text: str) -> str:
    """Very simple markdown → html for task body display."""
    lines = text.splitlines()
    out = []
    in_list = False
    in_code = False

    def flush_list():
        nonlocal in_list
        if in_list:
            out.append("</ul>")
            in_list = False

    for line in lines:
        # Code fence
        if line.strip().startswith("```"):
            flush_list()
            if in_code:
                out.append("</code></pre>")
                in_code = False
            else:
                lang = line.strip()[3:].strip()
                out.append(f'<pre><code class="lang-{lang}">')
                in_code = True
            continue
        if in_code:
            out.append(esc(line))
            continue

        s = line.rstrip()

        # Headings
        if s.startswith("#### "):
            flush_list(); out.append(f"<h4>{esc(s[5:])}</h4>"); continue
        if s.startswith("### "):
            flush_list(); out.append(f"<h3>{esc(s[4:])}</h3>"); continue
        if s.startswith("## "):
            flush_list(); out.append(f"<h2>{esc(s[3:])}</h2>"); continue
        if s.startswith("# "):
            flush_list(); out.append(f"<h1>{esc(s[2:])}</h1>"); continue

        # Checklist
        m = re.match(r'^(\s*)-\s*\[( |x|X)\]\s*(.*)', s)
        if m:
            checked = m.group(2).lower() == 'x'
            content = inline_md(m.group(3))
            chk = 'checked disabled' if checked else 'disabled'
            cls = ' class="done"' if checked else ''
            if not in_list:
                out.append("<ul class='checklist'>"); in_list = True
            out.append(f'<li{cls}><input type="checkbox" {chk}> {content}</li>')
            continue

        # Regular list
        m2 = re.match(r'^(\s*)-\s+(.*)', s)
        if m2:
            if not in_list:
                out.append("<ul>"); in_list = True
            out.append(f"<li>{inline_md(m2.group(2))}</li>")
            continue

        flush_list()

        # Horizontal rule
        if re.match(r'^[-*_]{3,}$', s.strip()):
            out.append("<hr>"); continue

        # Empty line
        if not s.strip():
            out.append("<br>"); continue

        # Paragraph
        out.append(f"<p>{inline_md(s)}</p>")

    flush_list()
    if in_code:
        out.append("</code></pre>")
    return "\n".join(out)


def esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def inline_md(s: str) -> str:
    s = esc(s)
    s = re.sub(r'\*\*(.+?)\*\*', r'<strong>\1</strong>', s)
    s = re.sub(r'\*(.+?)\*', r'<em>\1</em>', s)
    s = re.sub(r'`(.+?)`', r'<code>\1</code>', s)
    s = re.sub(r'\[([^\]]+)\]\(([^)]+)\)', r'<a href="\2" target="_blank">\1</a>', s)
    return s


# ---------------------------------------------------------------------------
# HTML
# ---------------------------------------------------------------------------

HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Meridian Board</title>
<style>
:root{--bg:#0d1117;--bg2:#161b22;--bg3:#21262d;--bg4:#2d333b;--border:#30363d;--text:#c9d1d9;--text2:#8b949e;--accent:#58a6ff;--green:#3fb950;--yellow:#d29922;--red:#f85149;--purple:#bc8cff;--orange:#ffa657;--cyan:#39d353}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--text);font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;font-size:13px;height:100vh;display:flex;flex-direction:column}
/* Header */
.hdr{background:var(--bg2);border-bottom:1px solid var(--border);padding:10px 16px;display:flex;align-items:center;gap:12px;flex-shrink:0}
.hdr h1{font-size:15px;font-weight:600}
.hdr-right{display:flex;align-items:center;gap:8px;margin-left:auto}
.pill-row{display:flex;gap:5px}
.pill{font-size:11px;padding:2px 9px;border-radius:12px;cursor:pointer;border:1px solid var(--border);background:var(--bg3);color:var(--text2);transition:all .15s;user-select:none}
.pill.active{border-color:var(--accent);color:var(--text);background:var(--bg4)}
.pill[data-a="philip"].active{border-color:#79b8ff;color:#79b8ff;background:#1f4a8a22}
.pill[data-a="fatih"].active{border-color:#85e89d;color:#85e89d;background:#1a3a2022}
.pill[data-a="matthew"].active{border-color:#e2a5ff;color:#e2a5ff;background:#3a1a3a22}
.search{background:var(--bg3);border:1px solid var(--border);color:var(--text);padding:4px 10px;border-radius:6px;font-size:12px;width:160px;outline:none}
.search:focus{border-color:var(--accent)}
.search::placeholder{color:var(--text2)}
.btn{background:var(--bg3);border:1px solid var(--border);color:var(--text);padding:4px 10px;border-radius:6px;cursor:pointer;font-size:12px}
.btn:hover{background:var(--bg4)}
/* Stats */
.stats{display:flex;gap:14px;padding:5px 16px;background:var(--bg2);border-bottom:1px solid var(--border);flex-shrink:0;flex-wrap:wrap}
.stat{font-size:11px;color:var(--text2)}
.stat b{color:var(--text)}
/* Board */
.board-wrap{flex:1;overflow-x:auto;overflow-y:hidden}
.board{display:flex;gap:10px;padding:12px;height:100%;align-items:flex-start}
/* Column */
.col{flex:0 0 232px;background:var(--bg2);border:1px solid var(--border);border-radius:10px;display:flex;flex-direction:column;max-height:100%}
.col.collapsed{flex:0 0 44px}
.col-hdr{padding:8px 10px;border-bottom:1px solid var(--border);display:flex;align-items:center;gap:7px;cursor:pointer;border-radius:10px 10px 0 0;user-select:none;flex-shrink:0}
.col.collapsed .col-hdr{border-bottom:none;border-radius:10px;writing-mode:vertical-rl;justify-content:center;padding:12px 6px}
.col-dot{width:7px;height:7px;border-radius:50%;flex-shrink:0}
.col-lbl{font-weight:600;font-size:11px;text-transform:uppercase;letter-spacing:.4px;color:var(--text2);flex:1}
.col-cnt{background:var(--bg3);border:1px solid var(--border);border-radius:10px;padding:1px 7px;font-size:10px;color:var(--text2);font-weight:600}
.col-body{padding:7px;overflow-y:auto;display:flex;flex-direction:column;gap:5px;flex:1}
.col.collapsed .col-body{display:none}
.col-empty{color:var(--text2);font-size:11px;text-align:center;padding:14px 0}
/* Column color dots */
.col-backlog .col-dot{background:var(--text2)}
.col-ready .col-dot{background:var(--accent)}
.col-in_progress .col-dot{background:var(--yellow)}
.col-review .col-dot{background:var(--purple)}
.col-waiting_human .col-dot{background:var(--orange)}
.col-done .col-dot{background:var(--green)}
.col-debt .col-dot{background:var(--red)}
/* Card */
.card{background:var(--bg3);border:1px solid var(--border);border-radius:7px;padding:8px 9px;cursor:pointer;transition:border-color .12s,box-shadow .12s;display:flex;flex-direction:column;gap:4px}
.card:hover{border-color:var(--accent);box-shadow:0 0 0 1px #58a6ff22}
.card-id{font-size:10px;color:var(--text2);font-family:monospace;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.card-title{font-size:12px;color:var(--text);line-height:1.4;font-weight:500}
.card-foot{display:flex;flex-wrap:wrap;gap:3px;align-items:center;margin-top:2px}
.badge{font-size:10px;padding:1px 6px;border-radius:10px;font-weight:500;white-space:nowrap}
.ba-philip{background:#1f4a8a22;color:#79b8ff;border:1px solid #1f4a8a55}
.ba-fatih{background:#1a3a2022;color:#85e89d;border:1px solid #1a3a2055}
.ba-matthew{background:#3a1a3a22;color:#e2a5ff;border:1px solid #3a1a3a55}
.ba-comp{background:#58a6ff22;color:var(--accent);border:1px solid #58a6ff33}
.ba-high{background:#f8514933;color:var(--red);border:1px solid #f8514944}
.ba-age{font-size:10px;color:var(--text2);margin-left:auto}
.progress-bar{height:3px;background:var(--bg4);border-radius:2px;margin-top:3px;overflow:hidden}
.progress-fill{height:100%;background:var(--green);border-radius:2px;transition:width .3s}

/* ===== Detail Panel ===== */
.overlay{display:none;position:fixed;inset:0;background:#00000088;z-index:200;backdrop-filter:blur(2px)}
.overlay.open{display:block}
.panel{position:fixed;right:0;top:0;bottom:0;width:520px;max-width:95vw;background:var(--bg2);border-left:1px solid var(--border);z-index:201;display:flex;flex-direction:column;transform:translateX(100%);transition:transform .2s ease}
.panel.open{transform:translateX(0)}
.panel-hdr{padding:14px 16px;border-bottom:1px solid var(--border);display:flex;align-items:flex-start;gap:10px;flex-shrink:0}
.panel-hdr-info{flex:1;min-width:0}
.panel-queue-badge{font-size:10px;padding:2px 8px;border-radius:10px;background:var(--bg3);border:1px solid var(--border);color:var(--text2);margin-bottom:6px;display:inline-block}
.panel-title{font-size:15px;font-weight:600;line-height:1.4;color:var(--text)}
.panel-id{font-size:11px;color:var(--text2);font-family:monospace;margin-top:3px}
.close-btn{background:none;border:none;color:var(--text2);font-size:18px;cursor:pointer;padding:2px 6px;border-radius:4px;flex-shrink:0}
.close-btn:hover{background:var(--bg3);color:var(--text)}
.panel-meta{padding:10px 16px;border-bottom:1px solid var(--border);display:flex;flex-wrap:wrap;gap:6px;flex-shrink:0}
.meta-chip{background:var(--bg3);border:1px solid var(--border);border-radius:6px;padding:3px 9px;font-size:11px;display:flex;gap:5px;align-items:center}
.meta-chip .lbl{color:var(--text2)}
.meta-chip .val{color:var(--text);font-weight:500}
.panel-body{flex:1;overflow-y:auto;padding:16px}
/* Rendered markdown */
.md h1{font-size:16px;font-weight:600;margin:14px 0 8px;color:var(--text);border-bottom:1px solid var(--border);padding-bottom:6px}
.md h2{font-size:14px;font-weight:600;margin:12px 0 6px;color:var(--text)}
.md h3{font-size:13px;font-weight:600;margin:10px 0 5px;color:var(--accent)}
.md h4{font-size:12px;font-weight:600;margin:8px 0 4px;color:var(--text2)}
.md p{margin:5px 0;line-height:1.6;color:var(--text)}
.md ul{margin:4px 0 4px 16px;display:flex;flex-direction:column;gap:3px}
.md li{line-height:1.5;color:var(--text)}
.md ul.checklist{list-style:none;margin-left:0}
.md ul.checklist li{display:flex;align-items:baseline;gap:6px;padding:2px 0}
.md ul.checklist li.done{color:var(--text2)}
.md ul.checklist li.done code{color:var(--text2)}
.md input[type=checkbox]{accent-color:var(--green);flex-shrink:0}
.md code{background:var(--bg3);border:1px solid var(--border);border-radius:4px;padding:1px 5px;font-family:monospace;font-size:11px;color:#e6edf3}
.md pre{background:var(--bg3);border:1px solid var(--border);border-radius:6px;padding:10px;overflow-x:auto;margin:8px 0}
.md pre code{background:none;border:none;padding:0;font-size:11px}
.md hr{border:none;border-top:1px solid var(--border);margin:12px 0}
.md strong{color:#e6edf3;font-weight:600}
.md em{color:var(--text2);font-style:italic}
.md a{color:var(--accent);text-decoration:none}
.md a:hover{text-decoration:underline}
.md br{display:block;margin:3px 0}
.panel-loading{color:var(--text2);text-align:center;padding:40px;font-size:13px}
/* Scrollbar */
::-webkit-scrollbar{width:5px;height:5px}
::-webkit-scrollbar-track{background:transparent}
::-webkit-scrollbar-thumb{background:var(--border);border-radius:3px}
::-webkit-scrollbar-thumb:hover{background:var(--text2)}
</style>
</head>
<body>

<div class="hdr">
  <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="#58a6ff" stroke-width="2"><circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/></svg>
  <h1>Meridian Board</h1>
  <div class="pill-row" id="agentFilter">
    <div class="pill active" data-a="all">All</div>
    <div class="pill" data-a="philip">Philip</div>
    <div class="pill" data-a="fatih">Fatih</div>
    <div class="pill" data-a="matthew">Matthew</div>
  </div>
  <div class="hdr-right">
    <input class="search" id="search" placeholder="Search…"/>
    <button class="btn" onclick="loadBoard()">↺ Refresh</button>
    <span style="font-size:11px;color:var(--text2)" id="ts"></span>
  </div>
</div>
<div class="stats" id="stats"></div>
<div class="board-wrap"><div class="board" id="board"></div></div>

<!-- Detail panel -->
<div class="overlay" id="overlay" onclick="closePanel()"></div>
<div class="panel" id="panel">
  <div class="panel-hdr">
    <div class="panel-hdr-info">
      <div class="panel-queue-badge" id="pBadge"></div>
      <div class="panel-title" id="pTitle">Loading…</div>
      <div class="panel-id" id="pId"></div>
    </div>
    <button class="close-btn" onclick="closePanel()">✕</button>
  </div>
  <div class="panel-meta" id="pMeta"></div>
  <div class="panel-body md" id="pBody"><div class="panel-loading">Loading…</div></div>
</div>

<script>
const Q_ORDER = ["backlog","ready","in_progress","review","waiting_human","done","debt"];
const Q_LABELS = {backlog:"Backlog",ready:"Ready",in_progress:"In Progress",review:"Review",waiting_human:"Waiting Human",done:"Done",debt:"Debt"};
const COLLAPSED_DEFAULT = new Set(["done","debt","backlog"]);

let boardData = {};
let activeAgent = "all";
let searchTerm = "";
let colCollapsed = new Set(COLLAPSED_DEFAULT);

async function loadBoard(){
  try{
    const r = await fetch("/api/board");
    boardData = await r.json();
    document.getElementById("ts").textContent = new Date().toLocaleTimeString();
    render();
  }catch(e){ document.getElementById("ts").textContent = "Error"; }
}

function matches(c){
  if(activeAgent !== "all" && c.agent !== activeAgent) return false;
  if(searchTerm){
    const h = (c.id+" "+c.title+" "+c.component).toLowerCase();
    if(!h.includes(searchTerm)) return false;
  }
  return true;
}

function render(){
  // Stats
  const total = Object.values(boardData).flat().length;
  const ready = (boardData.ready||[]).length;
  const ip = (boardData.in_progress||[]).length;
  const rev = (boardData.review||[]).length;
  const wh = (boardData.waiting_human||[]).length;
  const done = (boardData.done||[]).length;
  const debt = (boardData.debt||[]).length;
  document.getElementById("stats").innerHTML =
    s("Total",total)+s("Ready",ready,"var(--accent)")+s("In Progress",ip,"var(--yellow)")+
    s("Review",rev,"var(--purple)")+(wh?s("⚠ Waiting You",wh,"var(--orange)"):"")+"<span style='margin-left:auto'></span>"+
    s("Done",done,"var(--green)")+s("Debt",debt,"var(--red)");

  const board = document.getElementById("board");
  board.innerHTML="";
  for(const q of Q_ORDER){
    const all = boardData[q]||[];
    const vis = all.filter(matches);
    const collapsed = colCollapsed.has(q);
    const col = document.createElement("div");
    col.className = `col col-${q}${collapsed?" collapsed":""}`;
    col.innerHTML=`
      <div class="col-hdr" onclick="toggleCol('${q}')">
        <div class="col-dot"></div>
        <div class="col-lbl">${Q_LABELS[q]}</div>
        <div class="col-cnt">${vis.length}${vis.length<all.length?"/"+all.length:""}</div>
      </div>
      <div class="col-body" id="cb-${q}"></div>`;
    board.appendChild(col);
    if(!collapsed){
      const body = col.querySelector(".col-body");
      if(vis.length===0){
        body.innerHTML=`<div class="col-empty">—</div>`;
      } else {
        for(const c of vis) body.appendChild(makeCard(c));
      }
    }
  }
}

function s(label,val,color=""){
  return `<div class="stat">${label} <b${color?` style="color:${color}"`:""}">${val}</b></div>`;
}

function makeCard(c){
  const el=document.createElement("div");
  el.className="card";
  el.title=c.filename;
  el.onclick=()=>openPanel(c.filename,c.queue);

  const agBadge=c.agent?`<span class="badge ba-${c.agent}">${c.agent}</span>`:"";
  const compBadge=c.component?`<span class="badge ba-comp">${esc(c.component)}</span>`:"";
  const prioBadge=c.priority==="high"?`<span class="badge ba-high">high</span>`:"";

  let progressHtml="";
  const tot=c.done_items+c.todo_items;
  if(tot>0){
    const pct=Math.round(c.done_items/tot*100);
    progressHtml=`<div class="progress-bar"><div class="progress-fill" style="width:${pct}%"></div></div>`;
  }

  el.innerHTML=`
    <div class="card-id">${esc(c.id)}</div>
    <div class="card-title">${esc(c.title)}</div>
    ${progressHtml}
    <div class="card-foot">${agBadge}${compBadge}${prioBadge}<span class="ba-age">${esc(c.age)}</span></div>`;
  return el;
}

function toggleCol(q){
  if(colCollapsed.has(q)) colCollapsed.delete(q);
  else colCollapsed.add(q);
  render();
}

// ---- Detail panel ----
async function openPanel(filename,queue){
  document.getElementById("overlay").classList.add("open");
  document.getElementById("panel").classList.add("open");
  document.getElementById("pTitle").textContent="Loading…";
  document.getElementById("pId").textContent="";
  document.getElementById("pMeta").innerHTML="";
  document.getElementById("pBadge").textContent=Q_LABELS[queue]||queue;
  document.getElementById("pBody").innerHTML='<div class="panel-loading">Loading…</div>';

  try{
    const r = await fetch("/api/task/"+encodeURIComponent(filename));
    const t = await r.json();
    renderPanel(t);
  }catch(e){
    document.getElementById("pBody").innerHTML=`<div class="panel-loading">Error: ${e}</div>`;
  }
}

function renderPanel(t){
  document.getElementById("pTitle").textContent = t.title||t.id;
  document.getElementById("pId").textContent = t.id;

  // Meta chips
  const chips=[];
  if(t.assignee) chips.push(chip("Assignee",t.assignee));
  if(t.reviewer) chips.push(chip("Reviewer",t.reviewer));
  if(t.priority) chips.push(chip("Priority",t.priority));
  if(t.component) chips.push(chip("Component",t.component));
  if(t.created_at) chips.push(chip("Created",t.created_at));
  if(t.mtime) chips.push(chip("Modified",t.mtime));
  if(t.done_items+t.todo_items>0) chips.push(chip("Progress",t.done_items+"/"+(t.done_items+t.todo_items)+" done"));
  document.getElementById("pMeta").innerHTML=chips.join("");

  document.getElementById("pBody").innerHTML = t.body_html || "<em style='color:var(--text2)'>No content.</em>";
}

function chip(lbl,val){
  return `<div class="meta-chip"><span class="lbl">${esc(lbl)}</span><span class="val">${esc(String(val))}</span></div>`;
}

function closePanel(){
  document.getElementById("overlay").classList.remove("open");
  document.getElementById("panel").classList.remove("open");
}

// Keyboard ESC to close
document.addEventListener("keydown",e=>{ if(e.key==="Escape") closePanel(); });

// Agent filter
document.getElementById("agentFilter").addEventListener("click",e=>{
  const p=e.target.closest(".pill");
  if(!p) return;
  document.querySelectorAll(".pill").forEach(x=>x.classList.remove("active"));
  p.classList.add("active");
  activeAgent=p.dataset.a;
  render();
});

// Search
document.getElementById("search").addEventListener("input",e=>{
  searchTerm=e.target.value.toLowerCase();
  render();
});

function esc(s){ return String(s||"").replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;"); }

loadBoard();
setInterval(loadBoard,30000);
</script>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------

WORKSPACE: Path = Path("/home/umut/meridian")


class BoardHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path

        if path in ("/", "/board"):
            self._send(200, "text/html; charset=utf-8", HTML.encode())

        elif path == "/api/board":
            try:
                data = load_board(WORKSPACE)
                self._send(200, "application/json", json.dumps(data, ensure_ascii=False).encode())
            except Exception as e:
                self._send(500, "application/json", json.dumps({"error": str(e)}).encode())

        elif path.startswith("/api/task/"):
            filename = unquote(path[len("/api/task/"):])
            # Security: no path traversal
            if "/" in filename or "\\" in filename or ".." in filename:
                self._send(400, "application/json", b'{"error":"bad filename"}')
                return
            task_path = find_task_path(WORKSPACE, filename)
            if not task_path:
                self._send(404, "application/json", json.dumps({"error": f"Not found: {filename}"}).encode())
                return
            try:
                queue = task_path.parent.name.replace("-", "_")
                detail = read_task_detail(task_path, queue)
                detail["body_html"] = md_to_html(detail.pop("body", ""))
                detail.pop("raw", None)
                self._send(200, "application/json", json.dumps(detail, ensure_ascii=False).encode())
            except Exception as e:
                self._send(500, "application/json", json.dumps({"error": str(e)}).encode())

        else:
            self._send(404, "text/plain", b"Not found")

    def _send(self, code: int, ct: str, body: bytes):
        self.send_response(code)
        self.send_header("Content-Type", ct)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    global WORKSPACE
    ap = argparse.ArgumentParser(description="Meridian Board Server")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--workspace", default="/home/umut/meridian")
    ap.add_argument("--host", default="0.0.0.0")
    args = ap.parse_args()

    WORKSPACE = Path(args.workspace).expanduser().resolve()
    if not WORKSPACE.is_dir():
        print(f"ERROR: workspace not found: {WORKSPACE}", file=sys.stderr)
        sys.exit(1)

    server = HTTPServer((args.host, args.port), BoardHandler)
    print(f"🧭 Meridian Board  →  http://{args.host}:{args.port}/", flush=True)
    print(f"   Workspace: {WORKSPACE}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
