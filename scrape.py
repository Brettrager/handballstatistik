"""Fetch league data + match reports from handball4all and store them in SQLite.

Each match report is downloaded and parsed exactly once; the parse result is cached as
data/parsed/{sGID}.json (reports are not changed retroactively). Every run re-fetches only the
league JSON (results, standings, schedule) and rebuilds the database from the cached results.

Usage:
    python scrape.py              # incremental: only new reports are downloaded + parsed
    python scrape.py --reparse    # re-parse all reports (after parser changes; uses cached PDFs if present)
    python scrape.py --refresh    # re-download + re-parse all reports
"""
from __future__ import annotations

import argparse
import difflib
import hashlib
import json
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path

import requests

from parse_report import parse_report

ROOT = Path(__file__).parent
DATA = ROOT / "data"
REPORTS = DATA / "reports"
PARSED = DATA / "parsed"
DB_PATH = DATA / "handball.db"
API = "https://spo.handball4all.de/service/if_g_json.php"
REPORT_URL = "https://spo.handball4all.de/misc/sboPublicReports.php?sGID={}"
HEADERS = {"User-Agent": "handballstatistik-prototype/0.1 (private, non-commercial)"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS leagues (
    league_key TEXT PRIMARY KEY, og INTEGER, lid INTEGER, short TEXT, name TEXT, area TEXT,
    season TEXT, actualized TEXT, fetched_at TEXT, portal_url TEXT, sort_order INTEGER);
CREATE TABLE IF NOT EXISTS teams (
    team_id TEXT PRIMARY KEY, league_key TEXT, name TEXT, short_name TEXT);
CREATE TABLE IF NOT EXISTS standings (
    league_key TEXT, team_id TEXT, pos INTEGER, played INTEGER, won INTEGER, draw INTEGER, lost INTEGER,
    goals_for INTEGER, goals_against INTEGER, points_plus INTEGER, points_minus INTEGER,
    PRIMARY KEY (league_key, team_id));
CREATE TABLE IF NOT EXISTS games (
    gid TEXT PRIMARY KEY, sgid TEXT, league_key TEXT, game_no TEXT, date TEXT, time TEXT,
    home_team_id TEXT, guest_team_id TEXT, home_goals INTEGER, guest_goals INTEGER,
    home_half INTEGER, guest_half INTEGER, venue TEXT, town TEXT, spectators INTEGER,
    report_status TEXT);
CREATE TABLE IF NOT EXISTS players (
    player_id INTEGER PRIMARY KEY AUTOINCREMENT, team_id TEXT, name TEXT, UNIQUE (team_id, name));
CREATE TABLE IF NOT EXISTS appearances (
    gid TEXT, player_id INTEGER, team_id TEXT, side TEXT, number TEXT, goals INTEGER,
    sevenm_attempts INTEGER, sevenm_goals INTEGER, warning TEXT, penalties TEXT, penalty_count INTEGER,
    disq TEXT, report TEXT, number_change TEXT, PRIMARY KEY (gid, player_id));
CREATE TABLE IF NOT EXISTS officials (
    gid TEXT, team_id TEXT, code TEXT, name TEXT, warning TEXT, penalties TEXT, disq TEXT);
CREATE TABLE IF NOT EXISTS events (
    gid TEXT, seq INTEGER, clock TEXT, game_time TEXT, minute INTEGER, score_home INTEGER,
    score_guest INTEGER, type TEXT, side TEXT, team_id TEXT, player_id INTEGER, number TEXT, text TEXT,
    PRIMARY KEY (gid, seq));
CREATE TABLE IF NOT EXISTS report_warnings (gid TEXT, message TEXT);
"""


def iso_date(d: str) -> str:
    return datetime.strptime(d, "%d.%m.%y").strftime("%Y-%m-%d") if d else ""


def to_int(v):
    try:
        return int(str(v).strip())
    except (TypeError, ValueError):
        return None


def fetch_league(og: int, lid: int) -> dict:
    r = requests.get(API, params={"ca": 1, "cl": lid, "cmd": "ps", "og": og}, headers=HEADERS, timeout=30)
    r.raise_for_status()
    return r.json()[0]


PORTAL_URL = "https://www.handball4all.de/home/portal/baden-wuerttembergischer-hv#/league?ogId={og}&lId={lid}&allGames=1"


def discover_leagues(district: dict) -> list[dict]:
    """List a district's leagues via the H4A organisation endpoint, filtered by short-name prefixes."""
    r = requests.get(API, params={"ca": 0, "cmd": "po", "og": district["og"], "o": district["o"]},
                     headers=HEADERS, timeout=30)
    r.raise_for_status()
    data = r.json()
    data = data[0] if isinstance(data, list) else data
    include, exclude = tuple(district.get("include", [""])), tuple(district.get("exclude", []))
    found = []
    for c in data["content"]["classes"]:
        sname = c["gClassSname"]
        if sname.startswith(include) and not sname.startswith(exclude):
            lid = int(c["gClassID"])
            found.append({"og": district["og"], "lid": lid, "area": district.get("area", ""),
                          "portal_url": PORTAL_URL.format(og=district["og"], lid=lid)})
    print(f"District {district.get('area', district['o'])}: {len(found)} leagues")
    return found


def download_report(sgid: str, refresh: bool) -> Path | None:
    path = REPORTS / f"{sgid}.pdf"
    if path.exists() and not refresh:
        return path
    r = requests.get(REPORT_URL.format(sgid), headers=HEADERS, timeout=60)
    if r.status_code != 200 or not r.content.startswith(b"%PDF"):
        print(f"  ! report {sgid} not available ({r.status_code})")
        return None
    path.write_bytes(r.content)
    time.sleep(0.5)
    return path


STATS = {"cached": 0, "new": 0}


def get_report(g: dict, mode: str) -> dict | None:
    """Parsed report for a game: from the JSON cache, or downloaded + parsed once and cached."""
    sgid = g["sGID"]
    cache = PARSED / f"{sgid}.json"
    if cache.exists() and mode == "incremental":
        STATS["cached"] += 1
        return json.loads(cache.read_text(encoding="utf-8"))
    pdf = download_report(sgid, refresh=mode == "refresh")
    if not pdf:
        return None
    try:
        rep = parse_report(str(pdf), g["gHomeTeam"], g["gGuestTeam"])
    except Exception as exc:  # keep going; one broken report must not block the league
        print(f"  ! failed to parse {pdf.name}: {exc}")
        return None
    cache.write_text(json.dumps(rep, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    STATS["new"] += 1
    return rep


def resolve_team_ids(games: list[dict], teams: dict[str, str], parsed: dict[str, dict]) -> dict[str, str]:
    """Map the API's abbreviated team names to team IDs (report rosters carry the full names)."""
    full_to_id = {name: tid for tid, name in teams.items()}
    short_to_id: dict[str, str] = {name: tid for name, tid in full_to_id.items()}
    for g in games:
        rep = parsed.get(g["sGID"])
        if not rep:
            continue
        for short, full in ((g["gHomeTeam"], rep["home"]["team"]), (g["gGuestTeam"], rep["guest"]["team"])):
            if full in full_to_id:
                short_to_id[short] = full_to_id[full]
    unresolved = {s for g in games for s in (g["gHomeTeam"], g["gGuestTeam"])} - set(short_to_id)
    for short in unresolved:
        match = difflib.get_close_matches(short, list(full_to_id), n=1, cutoff=0.3)
        if match:
            short_to_id[short] = full_to_id[match[0]]
        else:
            print(f"  ! could not resolve team name '{short}'")
    return short_to_id


def store_league(con: sqlite3.Connection, cfg: dict, season: str, mode: str, order: int = 0) -> None:
    og, lid = cfg["og"], cfg["lid"]
    key = f"{og}-{lid}"
    if not cfg.get("portal_url"):
        cfg = {**cfg, "portal_url": PORTAL_URL.format(og=og, lid=lid)}
    data = fetch_league(og, lid)
    head, content = data["head"], data["content"]
    print(f"{head['sname']} – {head['name']}: {head.get('actualized', '')}")

    teams = {t["teamID"]: t["teamName"] for t in content["teamsList"]}
    games = content["futureGames"]["games"]

    parsed: dict[str, dict] = {}
    for g in games:
        if g.get("sGID") and (rep := get_report(g, mode)):
            parsed[g["sGID"]] = rep

    short_to_id = resolve_team_ids(games, teams, parsed)

    gids = [g["gID"] for g in games]
    old = [r[0] for r in con.execute("SELECT gid FROM games WHERE league_key=?", (key,))]
    for table in ("appearances", "officials", "events", "report_warnings"):
        con.executemany(f"DELETE FROM {table} WHERE gid=?", [(x,) for x in set(old) | set(gids)])
    for table in ("games", "standings", "teams"):
        con.execute(f"DELETE FROM {table} WHERE league_key=?", (key,))

    con.execute("INSERT OR REPLACE INTO leagues VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (key, og, lid, head["sname"], head["name"], cfg.get("area") or head.get("headline1"), season,
                 head.get("actualized", ""), datetime.now().isoformat(timespec="seconds"), cfg.get("portal_url", ""),
                 order))
    for tid, name in teams.items():
        short = next((s for s, i in short_to_id.items() if i == tid and s != name), name)
        con.execute("INSERT OR REPLACE INTO teams VALUES (?,?,?,?)", (tid, key, name, short))
    last_pos = None
    for s in content["score"]:
        last_pos = to_int(s["tabScore"]) or last_pos  # tied teams have an empty rank in the API
        con.execute("INSERT OR REPLACE INTO standings VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (key, s["tabTeamID"], last_pos, s["numPlayedGames"], s["numWonGames"],
                     s["numEqualGames"], s["numLostGames"], s["numGoalsShot"], s["numGoalsGot"],
                     s["pointsPlus"], s["pointsMinus"]))

    def player_id(team_id: str, name: str) -> int:
        # Deterministic ID so shared links (#/player/<id>) survive full database rebuilds
        pid = int(hashlib.sha1(f"{team_id}|{name}".encode()).hexdigest()[:10], 16)
        con.execute("INSERT OR IGNORE INTO players (player_id, team_id, name) VALUES (?,?,?)", (pid, team_id, name))
        return con.execute("SELECT player_id FROM players WHERE team_id=? AND name=?", (team_id, name)).fetchone()[0]

    n_reports = 0
    for g in games:
        gid, sgid = g["gID"], g.get("sGID") or ""
        rep = parsed.get(sgid)
        home_id, guest_id = short_to_id.get(g["gHomeTeam"]), short_to_id.get(g["gGuestTeam"])
        info = rep["info"] if rep else {}
        status = "parsed" if rep else ("missing" if sgid else "none")
        con.execute("INSERT OR REPLACE INTO games VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (gid, sgid, key, g["gNo"], iso_date(g["gDate"]), g["gTime"], home_id, guest_id,
                     to_int(g["gHomeGoals"]), to_int(g["gGuestGoals"]),
                     to_int(g.get("gHomeGoals_1")), to_int(g.get("gGuestGoals_1")),
                     g.get("gGymnasiumName"), g.get("gGymnasiumTown"), info.get("spectators"), status))
        if not rep:
            continue
        n_reports += 1
        side_team = {"home": home_id, "guest": guest_id}
        name_to_pid = {"home": {}, "guest": {}}
        for side in ("home", "guest"):
            tid = side_team[side]
            for e in rep[side]["entries"]:
                if e["official"]:
                    con.execute("INSERT INTO officials VALUES (?,?,?,?,?,?,?)",
                                (gid, tid, e["code"], e["name"], e["warning"], ",".join(e["penalties"]), e["disq"]))
                    continue
                pid = player_id(tid, e["name"])
                name_to_pid[side][e["name"]] = pid
                con.execute("INSERT OR REPLACE INTO appearances VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                            (gid, pid, tid, side, e["code"], e["goals"], e["sevenm_attempts"], e["sevenm_goals"],
                             e["warning"], ",".join(e["penalties"]), len(e["penalties"]), e["disq"], e["report"],
                             e["number_changes"]))
        for seq, ev in enumerate(rep["events"]):
            side = ev["side"]
            pid = name_to_pid.get(side, {}).get(ev["player"]) if side else None
            con.execute("INSERT INTO events VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (gid, seq, ev["clock"], ev["game_time"], ev["minute"], ev["score_home"], ev["score_guest"],
                         ev["type"], side, side_team.get(side) if side else None, pid, ev["number"], ev["text"]))
        for w in rep["warnings"]:
            con.execute("INSERT INTO report_warnings VALUES (?,?)", (gid, w))
            print(f"  ! game {g['gNo']}: {w}")
    print(f"  {len(games)} games, {n_reports} reports parsed")


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    grp = ap.add_mutually_exclusive_group()
    grp.add_argument("--refresh", action="store_true", help="re-download and re-parse all match reports")
    grp.add_argument("--reparse", action="store_true", help="re-parse all match reports (cached PDFs are reused)")
    args = ap.parse_args()
    mode = "refresh" if args.refresh else "reparse" if args.reparse else "incremental"

    REPORTS.mkdir(parents=True, exist_ok=True)
    PARSED.mkdir(parents=True, exist_ok=True)
    cfg = json.loads((ROOT / "leagues.json").read_text(encoding="utf-8-sig"))
    con = sqlite3.connect(DB_PATH)
    con.executescript(SCHEMA)
    if "sort_order" not in [r[1] for r in con.execute("PRAGMA table_info(leagues)")]:
        con.execute("ALTER TABLE leagues ADD COLUMN sort_order INTEGER")
    leagues = list(cfg.get("leagues", []))
    for district in cfg.get("districts", []):
        leagues += discover_leagues(district)
    seen, failed = set(), []
    for order, league in enumerate(leagues):
        key = f"{league['og']}-{league['lid']}"
        if key in seen:
            continue
        seen.add(key)
        try:
            store_league(con, league, cfg["season"], mode, order)
            con.commit()
        except Exception as exc:  # one failing league must not block the others
            con.rollback()
            failed.append(key)
            print(f"  ! league {key} failed: {exc}")
    stale = [r[0] for r in con.execute("SELECT league_key FROM leagues") if r[0] not in seen]
    for key in stale:
        gids = [(r[0],) for r in con.execute("SELECT gid FROM games WHERE league_key=?", (key,))]
        for table in ("appearances", "officials", "events", "report_warnings"):
            con.executemany(f"DELETE FROM {table} WHERE gid=?", gids)
        for table in ("games", "standings", "teams", "leagues"):
            con.execute(f"DELETE FROM {table} WHERE league_key=?", (key,))
    con.execute("DELETE FROM players WHERE player_id NOT IN (SELECT player_id FROM appearances)")
    con.commit()
    con.close()
    print(f"Reports: {STATS['new']} new, {STATS['cached']} from cache")
    print(f"Database: {DB_PATH}")
    if failed:
        sys.exit(f"{len(failed)} league(s) failed: {', '.join(failed)}")


if __name__ == "__main__":
    main()
