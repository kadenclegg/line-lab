#!/usr/bin/env python3
"""Line Lab PWA daily refresh.

Regenerates the gamesData block in index.html from the prediction journal,
commits, and pushes to GitHub Pages. Safe to run daily; pushes only when
data actually changed.
"""
import json
import re
import sqlite3
import subprocess
import sys
import urllib.request
from datetime import date, datetime, timezone
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

SB = Path.home() / "workspace" / "sports-bets"


def movement_points(con, sport, game_id, open_spread, open_total,
                    mkt_spread, mkt_total):
    """Chronological [spread, total] market points: open -> snapshots -> now."""
    pts = []
    if open_spread is not None or open_total is not None:
        pts.append([open_spread, open_total])
    for s, t in con.execute(
            "SELECT market_spread, market_total FROM market_snapshots"
            " WHERE sport=? AND game_id=? ORDER BY snapshot_at",
            (sport, game_id)):
        if s is None and t is None:
            continue
        if not pts or [s, t] != pts[-1]:
            pts.append([s, t])
    if mkt_spread is not None or mkt_total is not None:
        last = [mkt_spread, mkt_total]
        if not pts or last != pts[-1]:
            pts.append(last)
    if len(pts) < 2:
        return None
    return [[round(v, 2) if v is not None else None for v in p] for p in pts]


def attach_why(con, rows):
    """Attach per-game 'why this number' components, verified against the
    logged our_spread/our_total. Dropped silently on any mismatch so the
    explainer can never disagree with the displayed number."""
    sys.path.insert(0, str(SB))
    sys.path.insert(0, str(SB / "sports" / "cfb"))
    sys.path.insert(0, str(SB / "sports" / "nba"))
    explainers = {}
    try:
        import power_ratings_v2 as nfl_pr
        blob = json.load(open(SB / "power_ratings_v2.json"))
        explainers["nfl"] = (nfl_pr.explain_number, blob["teams"], blob["meta"])
    except Exception as e:
        print(f"nfl explainer unavailable: {e}", file=sys.stderr)
    try:
        import cfb_ratings_v2 as cfb_pr
        blob = json.load(open(SB / "sports" / "cfb" / "cfb_ratings_v2.json"))
        explainers["cfb"] = (cfb_pr.explain_number, blob["teams"], blob["meta"])
    except Exception as e:
        print(f"cfb explainer unavailable: {e}", file=sys.stderr)
    try:
        import nba_ratings as nba_pr
        blob = json.load(open(SB / "sports" / "nba" / "nba_ratings.json"))
        explainers["nba"] = (nba_pr.explain_number, blob["teams"], blob["meta"])
    except Exception as e:
        print(f"nba explainer unavailable: {e}", file=sys.stderr)
    if not explainers:
        return
    notes = {gid: (nt or "") for gid, nt in con.execute(
        "SELECT game_id, notes FROM predictions WHERE sport IN ('cfb','nba')")}
    n = 0
    for r in rows:
        ex = explainers.get(r["sport"])
        if not ex or r["our_spread"] is None or r["our_total"] is None:
            continue
        fn, ratings, meta = ex
        try:
            kw = {"neutral": "neutral site" in notes.get(r["game_id"], "")} \
                if r["sport"] in ("cfb", "nba") else {}
            w = fn(r["away_team"], r["home_team"], ratings, meta, **kw)
        except Exception:
            continue
        if (abs(w["our_spread"] - r["our_spread"]) <= 0.15
                and abs(w["our_total"] - r["our_total"]) <= 0.15):
            r["why"] = {
                "pa": w["proj_away"], "ph": w["proj_home"],
                "sp": [[p["label"], p["team"], round(p["pts"], 2)]
                       for p in w["spread_parts"]],
                "tp": [[p["label"], round(p["pts"], 2)]
                       for p in w["total_parts"]],
            }
            n += 1
    print(f"explainers attached: {n}")


# ---------------------------------------------------------------------------
# Injuries (ESPN site API) + closing lines + team bias.
# ESPN 403s browser/custom user agents on these endpoints, so urllib's
# DEFAULT user agent must be used (no custom UA header). Injury fetching is
# best-effort: any failure degrades to an empty/partial payload and never
# breaks the refresh.
# ---------------------------------------------------------------------------
INJ_LEAGUES = {
    "nfl": "football/nfl",
    "cfb": "football/college-football",
    "nba": "basketball/nba",
    "mlb": "baseball/mlb",
}
ESPN = "https://site.api.espn.com/apis/site/v2/sports"

# Statuses that move numbers. Normalized case-insensitively; anything else
# (probable, etc.) is dropped.
_INJ_KEEP = {
    "out": "Out",
    "doubtful": "Doubtful",
    "questionable": "Questionable",
    "injured reserve": "Injured Reserve",
    "ir": "Injured Reserve",
    "suspended": "Suspended",
    "suspension": "Suspended",
    "day-to-day": "Day-To-Day",
    "day to day": "Day-To-Day",
    "dtd": "Day-To-Day",
}


def _inj_get(url, timeout=20):
    # NOTE: no custom User-Agent — ESPN 403s non-default UAs here.
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.load(r)


def _norm_inj_status(s):
    return _INJ_KEEP.get((s or "").strip().lower())


def fetch_injuries():
    """Return {sport: {abbr: [{p, s, d, det}]}, '_asof': iso, '_leagues': {...}}.

    p=player, s=status, d=ESPN report date (YYYY-MM-DD), det=injury detail.
    Never raises: per-league failures are recorded under _leagues and the
    refresh continues with whatever was fetched.
    """
    out = {"_asof": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
           "_leagues": {}}
    for sport, league in INJ_LEAGUES.items():
        try:
            teams = _inj_get(f"{ESPN}/{league}/teams?limit=1000")
            abbr = {}
            for t in teams["sports"][0]["leagues"][0]["teams"]:
                tm = t["team"]
                abbr[str(tm["id"])] = tm["abbreviation"]
            data = _inj_get(f"{ESPN}/{league}/injuries")
            n_teams, n_entries = 0, 0
            for entry in data.get("injuries", []):
                a = abbr.get(str(entry.get("id")), entry.get("displayName"))
                if not a:
                    continue
                kept = []
                for inj in entry.get("injuries", []) or []:
                    s = _norm_inj_status(inj.get("status"))
                    if not s:
                        continue
                    ath = inj.get("athlete") or {}
                    det = (inj.get("details") or {}).get("type") or ""
                    d = (inj.get("date") or "")[:10]
                    kept.append({
                        "p": ath.get("displayName") or "Unknown",
                        "s": s,
                        "d": d,
                        "det": det,
                    })
                if kept:
                    # Most severe first: Out/IR/Suspended, then Doubtful,
                    # then Questionable/Day-To-Day.
                    order = {"Out": 0, "Injured Reserve": 0, "Suspended": 0,
                             "Doubtful": 1, "Questionable": 2, "Day-To-Day": 2}
                    kept.sort(key=lambda e: (order.get(e["s"], 3), e["p"]))
                    out.setdefault(sport, {})[a] = kept
                    n_teams += 1
                    n_entries += len(kept)
            out["_leagues"][sport] = {"teams": n_teams, "entries": n_entries}
        except Exception as e:  # best-effort: never break the refresh
            out["_leagues"][sport] = {"error": f"{type(e).__name__}: {e}"[:120]}
            print(f"injuries {sport} failed: {e}", file=sys.stderr)
    return out


def attach_close(con, rows):
    """Attach per-game closing line proxy: the latest market snapshot
    (falls back to the journal's current market line). Stored as
    r['close'] = {'sp':..., 'tp':...} for the CLV drawer section."""
    n = 0
    for r in rows:
        row = con.execute(
            "SELECT market_spread, market_total FROM market_snapshots"
            " WHERE sport=? AND game_id=? ORDER BY snapshot_at DESC LIMIT 1",
            (r["sport"], r["game_id"])).fetchone()
        sp, tp = (row[0], row[1]) if row else (None, None)
        if sp is None:
            sp = r.get("market_spread")
        if tp is None:
            tp = r.get("market_total")
        if sp is not None or tp is not None:
            r["close"] = {"sp": sp, "tp": tp}
            n += 1
    print(f"closing lines attached: {n}")


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
    """Replace `const <var>=[...];` or `const <var>={...};` (bracket-matched,
    string-aware) with new JSON. Works for array and object literals."""
    marker = f"const {var}="
    i = script.find(marker)
    if i == -1:
        raise ValueError(f"literal {var} not found")
    start = i + len(marker)
    pairs = {"[": "]", "{": "}", "(": ")"}
    if script[start] not in pairs:
        raise ValueError(f"literal {var} has unexpected opener {script[start]!r}")
    stack, instr, esc, q, j = [], False, False, "", start
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
            elif c in pairs:
                stack.append(pairs[c])
            elif c in "]})":
                if not stack or c != stack[-1]:
                    raise ValueError(f"literal {var}: unbalanced brackets")
                stack.pop()
                if not stack:
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

    con = sqlite3.connect(DB)
    try:
        for r in rows:
            mv = movement_points(con, r["sport"], r["game_id"],
                                r.get("open_spread"), r.get("open_total"),
                                r.get("market_spread"), r.get("market_total"))
            if mv:
                r["mv"] = mv
        attach_why(con, rows)
        attach_close(con, rows)
    finally:
        con.close()

    # Team bias (historical model tendency) and injury flags, embedded as
    # literals. Both tolerate missing/failed sources — the app guards on
    # undefined and simply hides those drawer sections.
    try:
        team_bias = json.load(open(HERE / "teambias.json"))
    except Exception as e:
        print(f"teambias.json unavailable: {e}", file=sys.stderr)
        team_bias = {}
    injury_data = fetch_injuries()

    data_json = json.dumps(rows, separators=(",", ":"))
    new_script = replace_literal(m.group(1), "gamesData", data_json)
    for var, payload in (("teamBias", team_bias), ("injuryData", injury_data)):
        try:
            new_script = replace_literal(
                new_script, var, json.dumps(payload, separators=(",", ":")))
        except ValueError as e:
            print(f"warning: {e} — leaving placeholder", file=sys.stderr)

    # Refresh the TODAY marker so headers/labels stay current.
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
