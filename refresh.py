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

COLS = ["id", "sport", "game_date", "away_team", "home_team", "our_spread",
        "market_spread", "our_total", "market_total", "home_score",
        "away_score", "result", "model_version"]


def fetch_rows():
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        f"SELECT {', '.join(COLS)} FROM predictions ORDER BY game_date, sport, id"
    ).fetchall()
    con.close()
    return [dict(r) for r in rows]


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
    data_json = json.dumps(rows, separators=(",", ":"))
    html = INDEX.read_text()

    m = __import__("re").search(r"<script>(.*?)</script>", html, __import__("re").S)
    if not m:
        print("no inline script found", file=sys.stderr)
        return 1
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
    push = subprocess.run(["git", "push", "origin", "main"], cwd=HERE,
                          capture_output=True, text=True)
    if push.returncode != 0:
        print(push.stderr[-2000:], file=sys.stderr)
        return 1
    print(f"pushed refresh: {n} games")
    return 0


if __name__ == "__main__":
    sys.exit(main())
