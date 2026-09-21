#!/usr/bin/env python3
"""Line Lab PWA daily refresh.

Regenerates the gamesData block in index.html from the prediction journal,
commits, and pushes to GitHub Pages. Safe to run daily; pushes only when
data actually changed.
"""
import json
import sqlite3
import subprocess
import sys
from datetime import date
from pathlib import Path

HERE = Path(__file__).resolve().parent
INDEX = HERE / "index.html"
DB = Path.home() / "workspace" / "sports-bets" / "journal" / "predictions.db"

COLS = ["id", "sport", "game_id", "game_date", "away_team", "home_team",
        "our_spread", "market_spread", "our_total", "market_total",
        "home_score", "away_score", "result", "model_version"]

# Display-only enrichment fields hand-maintained in index.html (logos, colors,
# kickoff times, records, opening lines, AP ranks). The journal doesn't carry
# them, so refresh.py must preserve them across data rebuilds instead of
# wiping them out.
ENRICH_FIELDS = ["away_logo", "home_logo", "away_color", "home_color",
                 "away_rank", "home_rank", "kickoff_utc", "away_record",
                 "home_record", "open_spread", "open_total"]

# AP ranks only exist for college sports. Team abbreviations collide across
# sports (e.g. MLB TEX Rangers vs CFB Texas #1), so never carry a rank onto a
# non-college game.
COLLEGE_SPORTS = ("cfb", "cbb")


def fetch_rows():
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        f"SELECT {', '.join(COLS)} FROM predictions ORDER BY game_date, sport, id"
    ).fetchall()
    con.close()
    return [dict(r) for r in rows]


def parse_existing_games(script: str) -> dict:
    """Return {game_id: enrichment-dict} from the current gamesData literal."""
    marker = "const gamesData=["
    i = script.find(marker)
    if i == -1:
        return {}
    start = i + len(marker) - 1
    depth, instr, esc, q, j = 0, False, False, "", start
    while j < len(script):
        c = script[j]
        if instr:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == q:
                instr = False
        else:
            if c in "\"'":
                instr, q = True, c
            elif c == "[":
                depth += 1
            elif c == "]":
                depth -= 1
                if depth == 0:
                    break
        j += 1
    try:
        games = json.loads(script[start:j + 1])
    except json.JSONDecodeError:
        return {}
    out = {}
    for g in games:
        gid = g.get("game_id")
        if not gid:
            continue
        keep = {k: g.get(k) for k in ENRICH_FIELDS if g.get(k) is not None}
        if g.get("sport") not in COLLEGE_SPORTS:
            keep.pop("away_rank", None)
            keep.pop("home_rank", None)
        out[gid] = keep
    return out


def replace_literal(script: str, var: str, new_json: str) -> str:
    """Replace `const <var>=[...];` (bracket-matched) with new JSON."""
    marker = f"const {var}=["
    i = script.find(marker)
    if i == -1:
        raise ValueError(f"literal {var} not found")
    start = i + len(marker) - 1  # at '['
    depth, instr, esc, q, j = 0, False, False, "", start
    while j < len(script):
        c = script[j]
        if instr:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == q:
                instr = False
        else:
            if c in "\"'":
                instr, q = True, c
            elif c == "[":
                depth += 1
            elif c == "]":
                depth -= 1
                if depth == 0:
                    break
        j += 1
    return script[:start] + new_json + script[j + 1:]


def main() -> int:
    rows = fetch_rows()
    html = INDEX.read_text()

    m = __import__("re").search(r"<script>(.*?)</script>", html, __import__("re").S)
    if not m:
        print("no inline script found", file=sys.stderr)
        return 1

    # Preserve hand-maintained display enrichment keyed by game_id so a data
    # rebuild never wipes logos/colors/kickoffs/records/ranks, and strip any
    # AP ranks that leaked onto non-college games (abbreviation collisions).
    enrich = parse_existing_games(m.group(1))
    merged = 0
    for r in rows:
        extra = enrich.get(r["game_id"])
        if extra:
            r.update(extra)
            merged += 1

    data_json = json.dumps(rows, separators=(",", ":"))
    new_script = replace_literal(m.group(1), "gamesData", data_json)

    # Refresh the TODAY marker so headers/labels stay current.
    import re
    new_script = re.sub(r"const TODAY='[^']*'",
                        f"const TODAY='{date.today().isoformat()}'", new_script, count=1)

    new_html = html[:m.start(1)] + new_script + html[m.end(1):]
    if new_html == html:
        print("no changes")
        return 0
    INDEX.write_text(new_html)

    subprocess.run(["git", "add", "index.html"], cwd=HERE, check=True)
    status = subprocess.run(["git", "status", "--porcelain"], cwd=HERE,
                            capture_output=True, text=True, check=True).stdout.strip()
    if not status:
        print("no changes to commit")
        return 0
    n = len(rows)
    subprocess.run(["git", "commit", "-m", f"daily data refresh: {n} games ({date.today().isoformat()})"],
                   cwd=HERE, check=True)
    # Direct push is network-blocked from this host; publishing goes through a
    # browser task, so a failed push is a warning, not an error.
    push = subprocess.run(["git", "push", "origin", "main"], cwd=HERE,
                          capture_output=True, text=True)
    if push.returncode != 0:
        print("push blocked from this host; publish index.html via browser task")
    print(f"refreshed: {n} games, kept enrichment on {merged}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
