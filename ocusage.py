#!/usr/bin/env python3
"""ocusage - what your OpenCode sessions actually cost you in tokens.

Reads the local OpenCode database read only. Nothing is written, nothing leaves
the machine.

    python ocusage.py                  # everything, grouped by project
    python ocusage.py --since 7d
    python ocusage.py --by session --top 20
    python ocusage.py --by day
    python ocusage.py --json
"""

import argparse
import json
import os
import re
import sqlite3
import sys
from datetime import datetime, timedelta

DEFAULT_DB = "~/.local/share/opencode/opencode.db"

# The /usage slash command. %(script)s is filled in with the real path so the
# user never has to edit a path by hand.
COMMAND_TEMPLATE = """---
description: Show OpenCode token usage from the local database
---
Run this exact command with the shell tool and show its output to the user verbatim, in a code block, without summarising or reformatting it:

```
python "%(script)s" $ARGUMENTS
```

If it fails, show the error as-is. Do not invent numbers and do not re-implement the query yourself.
"""


def install_command(dest=None):
    """Write the /usage command for this copy of the script."""
    script = os.path.abspath(__file__)
    target_dir = dest or os.path.expanduser("~/.config/opencode/command")
    try:
        os.makedirs(target_dir, exist_ok=True)
        target = os.path.join(target_dir, "usage.md")
        with open(target, "w", encoding="utf-8") as fh:
            fh.write(COMMAND_TEMPLATE % {"script": script})
    except OSError as exc:
        sys.exit("Could not write the command file: %s" % exc)
    print("Wrote %s" % target)
    print("Restart OpenCode, then type /usage (or /usage --since 7d).")


def find_db(explicit=None):
    if explicit:
        return os.path.expanduser(explicit)
    env = os.environ.get("OPENCODE_DB")
    if env:
        return os.path.expanduser(env)
    # Linux and macOS keep it under the data dir; the default covers Windows too.
    for alt in (DEFAULT_DB, "~/Library/Application Support/opencode/opencode.db"):
        p = os.path.expanduser(alt)
        if os.path.exists(p):
            return p
    return os.path.expanduser(DEFAULT_DB)


def connect(path):
    if not os.path.exists(path):
        sys.exit("No OpenCode database at %s\nPoint at one with --db." % path)
    uri = "file:" + path.replace("\\", "/") + "?mode=ro"
    try:
        con = sqlite3.connect(uri, uri=True, timeout=5)
        con.row_factory = sqlite3.Row
        # Fail here, with a readable message, rather than half way through a
        # report on a file that is not an OpenCode database.
        con.execute("select 1 from session_v2 limit 1").fetchone()
    except sqlite3.Error as exc:
        sys.exit(
            "%s is not a readable OpenCode database (%s).\n"
            "If OpenCode changed its schema, this tool needs updating." % (path, exc)
        )
    return con


def model_name(raw):
    if not raw:
        return "unknown"
    try:
        m = json.loads(raw)
    except (ValueError, TypeError):
        return str(raw)
    if isinstance(m, dict):
        pid = m.get("providerID") or m.get("provider") or ""
        mid = m.get("id") or m.get("modelID") or ""
        return ("%s/%s" % (pid, mid)) if pid else (mid or "unknown")
    return str(m)


def load(con):
    msg_counts = {}
    try:
        for r in con.execute(
            "select session_id, count(*) as n from session_message where type='assistant' group by session_id"
        ):
            msg_counts[r["session_id"]] = r["n"]
    except sqlite3.Error:
        pass

    projects = {}
    try:
        for r in con.execute("select id, name, worktree from project"):
            label = r["name"] or (os.path.basename((r["worktree"] or "").rstrip("/\\")) or "unknown")
            projects[r["id"]] = label
    except sqlite3.Error:
        pass

    rows = []
    for r in con.execute(
        "select id, project_id, title, directory, model, agent, cost, "
        "tokens_input, tokens_output, tokens_reasoning, tokens_cache_read, tokens_cache_write, "
        "time_created, time_updated from session_v2"
    ):
        cache_read = r["tokens_cache_read"] or 0
        cache_write = r["tokens_cache_write"] or 0
        tin = r["tokens_input"] or 0
        tout = r["tokens_output"] or 0
        treason = r["tokens_reasoning"] or 0
        total = tin + tout + treason + cache_read + cache_write
        rows.append(
            {
                "id": r["id"],
                "title": r["title"] or "",
                "project": projects.get(r["project_id"]) or (os.path.basename((r["directory"] or "").rstrip("/\\")) or "unknown"),
                "directory": r["directory"] or "",
                "model": model_name(r["model"]),
                "agent": r["agent"] or "",
                "cost": r["cost"] or 0.0,
                "in": tin,
                "out": tout,
                "reasoning": treason,
                "cache_read": cache_read,
                "cache_write": cache_write,
                "total": total,
                "messages": msg_counts.get(r["id"], 0),
                "created": (r["time_created"] or 0) / 1000.0,
                "updated": (r["time_updated"] or 0) / 1000.0,
            }
        )
    return rows


def cutoff(spec, now=None):
    now = now or datetime.now()
    if not spec or spec == "all":
        return None
    m = re.fullmatch(r"(\d+)([hdwm])", spec.strip().lower())
    if m:
        n, unit = int(m.group(1)), m.group(2)
        delta = {
            "h": timedelta(hours=n),
            "d": timedelta(days=n),
            "w": timedelta(weeks=n),
            "m": timedelta(days=30 * n),
        }[unit]
        return (now - delta).timestamp()
    try:
        return datetime.fromisoformat(spec).timestamp()
    except ValueError:
        sys.exit("Cannot read --since %r. Try 7d, 24h, 2w, or 2026-11-01." % spec)


def human(n):
    if n < 1000:
        return "%d" % n
    # Round before picking the unit, so 999999 is 1.0 M and not 1000.0 K.
    if n >= 999_500_000:
        return "%.1f B" % (n / 1_000_000_000)
    if n >= 999_500:
        return "%.1f M" % (n / 1_000_000)
    return "%.1f K" % (n / 1_000)


def money(n):
    return "$0.00" if not n else ("$%.2f" % n if n >= 0.01 else "<$0.01")


def group(rows, key):
    buckets = {}
    for r in rows:
        if key == "project":
            k = r["project"]
        elif key == "model":
            k = r["model"]
        elif key == "agent":
            k = r["agent"] or "unknown"
        elif key == "session":
            k = r["title"] or r["id"][:16]
        elif key == "day":
            k = datetime.fromtimestamp(r["created"]).strftime("%Y-%m-%d") if r["created"] else "unknown"
        else:
            k = "all"
        b = buckets.setdefault(
            k,
            {"name": k, "sessions": 0, "messages": 0, "in": 0, "out": 0, "reasoning": 0,
             "cache_read": 0, "cache_write": 0, "total": 0, "cost": 0.0},
        )
        b["sessions"] += 1
        for f in ("messages", "in", "out", "reasoning", "cache_read", "cache_write", "total"):
            b[f] += r[f]
        b["cost"] += r["cost"]
    return sorted(buckets.values(), key=lambda b: b["total"], reverse=True)


def totals(rows):
    t = {"sessions": len(rows), "messages": 0, "in": 0, "out": 0, "reasoning": 0,
         "cache_read": 0, "cache_write": 0, "total": 0, "cost": 0.0}
    for r in rows:
        for f in ("messages", "in", "out", "reasoning", "cache_read", "cache_write", "total"):
            t[f] += r[f]
        t["cost"] += r["cost"]
    return t


def pct(part, whole):
    return "0%" if not whole else "%d%%" % round(100.0 * part / whole)


def main():
    ap = argparse.ArgumentParser(description="What your OpenCode sessions cost in tokens.")
    ap.add_argument("--db", help="path to opencode.db")
    ap.add_argument("--since", default="all", help="7d, 24h, 2w, or 2026-11-01")
    ap.add_argument("--by", default="project", choices=["project", "model", "agent", "session", "day"])
    ap.add_argument("--top", type=int, default=12)
    ap.add_argument("--json", action="store_true")
    ap.add_argument(
        "--install-command",
        nargs="?",
        const="",
        default=None,
        metavar="DIR",
        help="write the /usage slash command into the OpenCode command folder",
    )
    args = ap.parse_args()

    if args.install_command is not None:
        install_command(args.install_command or None)
        return

    con = connect(find_db(args.db))
    rows = load(con)
    cut = cutoff(args.since)
    if cut:
        rows = [r for r in rows if r["created"] >= cut]

    if not rows:
        print("No sessions in that window.")
        return

    t = totals(rows)
    groups = group(rows, args.by)

    if args.json:
        print(json.dumps({"totals": t, "groups": groups[: args.top]}, indent=2))
        return

    print()
    print("ocusage  ·  opencode token usage")
    print()
    print("  %d sessions  ·  %d messages  ·  %d projects" % (t["sessions"], t["messages"], len({r["project"] for r in rows})))
    print()
    print("  input        %10s" % human(t["in"]))
    print("  output       %10s" % human(t["out"]))
    if t["reasoning"]:
        print("  reasoning    %10s" % human(t["reasoning"]))
    print("  cache read   %10s   %s of everything" % (human(t["cache_read"]), pct(t["cache_read"], t["total"])))
    print("  cache write  %10s" % human(t["cache_write"]))
    print("  %-12s %10s" % ("total", human(t["total"])))
    print("  %-12s %10s" % ("cost", money(t["cost"])))
    print()

    biggest = max(rows, key=lambda r: r["total"])
    busiest = max(rows, key=lambda r: r["messages"])
    print("  biggest session  %s  (%s, %s messages)" % (human(biggest["total"]), biggest["title"][:40] or biggest["id"][:14], biggest["messages"]))
    print("  most turns       %s  (%s, %s tokens)" % (busiest["messages"], busiest["title"][:40] or busiest["id"][:14], human(busiest["total"])))
    print()

    label = {"project": "PROJECT", "model": "MODEL", "agent": "AGENT", "session": "SESSION", "day": "DAY"}[args.by]
    # One session is one row, so counting sessions there says nothing. Show the
    # number of turns instead, which is what makes a session expensive.
    count_label, count_key = ("MSGS", "messages") if args.by == "session" else ("SESS", "sessions")
    print("  by %s" % args.by)
    print("  %-9s %-8s %-8s %-8s %-6s %-5s %s" % ("TOTAL", "IN", "OUT", "CACHE", "SHARE", count_label, label))
    print("  " + "-" * 96)
    for b in groups[: args.top]:
        name = b["name"]
        if len(name) > 44:
            name = name[:41] + "..."
        print(
            "  %-9s %-8s %-8s %-8s %-6s %-5d %s"
            % (human(b["total"]), human(b["in"]), human(b["out"]), human(b["cache_read"]),
               pct(b["cache_read"], b["total"]), b[count_key], name)
        )
    if len(groups) > args.top:
        print("  ... %d more, raise --top" % (len(groups) - args.top))
    print()


if __name__ == "__main__":
    main()
