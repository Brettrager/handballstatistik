"""Export the SQLite data into a self-contained static HTML site.

Usage:
    python build_site.py [output_dir]
"""
from __future__ import annotations

import json
import shutil
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).parent
DB_PATH = ROOT / "data" / "handball.db"
TEMPLATE = ROOT / "site_template.html"
STATIC = ROOT / "static"
DEFAULT_OUT = ROOT.parent.parent / "04_Deliverables" / "Handballstatistiken"
REPORT_URL = "https://spo.handball4all.de/misc/sboPublicReports.php?sGID="


def rows(con, sql, *args):
    con.row_factory = sqlite3.Row
    return [dict(r) for r in con.execute(sql, args)]


def export(con: sqlite3.Connection) -> dict:
    leagues = []
    for lg in rows(con, "SELECT * FROM leagues ORDER BY sort_order, short"):
        key = lg["league_key"]
        teams = rows(con, """SELECT t.team_id id, t.name, t.short_name short, s.pos, s.played, s.won, s.draw, s.lost,
                                    s.goals_for gf, s.goals_against ga, s.points_plus pp, s.points_minus pm
                             FROM teams t LEFT JOIN standings s ON s.team_id=t.team_id AND s.league_key=t.league_key
                             WHERE t.league_key=? ORDER BY s.pos IS NULL, s.pos, s.points_plus - s.points_minus DESC, s.goals_for - s.goals_against DESC, t.name""", key)
        players = rows(con, """SELECT DISTINCT p.player_id id, p.name, p.team_id team FROM players p
                               JOIN appearances a ON a.player_id=p.player_id
                               JOIN games g ON g.gid=a.gid WHERE g.league_key=?""", key)
        games = []
        for g in rows(con, "SELECT * FROM games WHERE league_key=? ORDER BY date, time, game_no", key):
            game = {
                "id": g["gid"], "no": g["game_no"], "date": g["date"], "time": g["time"],
                "home": g["home_team_id"], "guest": g["guest_team_id"],
                "hg": g["home_goals"], "gg": g["guest_goals"], "hh": g["home_half"], "gh": g["guest_half"],
                "venue": g["venue"], "town": g["town"], "spectators": g["spectators"],
                "report": REPORT_URL + g["sgid"] if g["sgid"] else None,
                "parsed": g["report_status"] == "parsed",
            }
            if game["parsed"]:
                game["lineups"] = {"home": [], "guest": []}
                for a in rows(con, "SELECT * FROM appearances WHERE gid=? ORDER BY CAST(number AS INTEGER)", g["gid"]):
                    game["lineups"][a["side"]].append({
                        "p": a["player_id"], "nr": a["number"], "g": a["goals"], "sa": a["sevenm_attempts"],
                        "sg": a["sevenm_goals"], "w": 1 if a["warning"] else 0, "pen": a["penalty_count"],
                        "d": 1 if a["disq"] else 0,
                    })
                game["officials"] = {"home": [], "guest": []}
                for o in rows(con, "SELECT * FROM officials WHERE gid=? ORDER BY code", g["gid"]):
                    side = "home" if o["team_id"] == g["home_team_id"] else "guest"
                    game["officials"][side].append({"code": o["code"], "name": o["name"]})
                game["events"] = [
                    [e["game_time"], e["score_home"], e["score_guest"], e["type"], e["side"], e["player_id"],
                     e["number"], e["text"] if e["type"] == "other" else None]
                    for e in rows(con, "SELECT * FROM events WHERE gid=? ORDER BY seq", g["gid"])
                ]
            games.append(game)
        leagues.append({
            "key": key, "short": lg["short"], "name": lg["name"], "area": lg["area"], "season": lg["season"],
            "actualized": lg["actualized"], "fetched": lg["fetched_at"], "portal": lg["portal_url"],
            "nav_end": bool(lg["nav_end"]),
            "teams": teams, "players": players, "games": games,
        })
    return {"generated": datetime.now().isoformat(timespec="minutes"), "leagues": leagues}


def _de(n: float) -> str:
    return f"{n:.2f}".replace(".", ",")


def _fdate(iso: str) -> str:
    y, m, d = iso.split("-")
    return f"{d}.{m}.{y[2:]}"


def _static_league(lg: dict, e) -> str:
    teams = {t["id"]: t for t in lg["teams"]}
    names = {p["id"]: p for p in lg["players"]}
    tshort = lambda tid: teams[tid]["short"] if tid in teams else "?"
    agg: dict = {}
    for g in lg["games"]:
        if not g.get("parsed"):
            continue
        for side in ("home", "guest"):
            for a in g["lineups"][side]:
                s = agg.setdefault(a["p"], dict(sp=0, t=0, sa=0, sg=0, pen=0, w=0, d=0))
                s["sp"] += 1; s["t"] += a["g"] or 0; s["sa"] += a["sa"] or 0; s["sg"] += a["sg"] or 0
                s["pen"] += a["pen"] or 0; s["w"] += a["w"]; s["d"] += a["d"]
    ranked = sorted(agg.items(), key=lambda kv: (-kv[1]["t"], kv[1]["sp"], names[kv[0]]["name"]))
    prow = [
        f"<tr><td>{i}</td><td class='name'><b>{e(names[pid]['name'])}</b><br><span class='tm'>{e(tshort(names[pid]['team']))}</span></td>"
        f"<td>{s['sp']}</td><td class='sortcol'>{s['t']}</td><td>{_de(s['t'] / s['sp'])}</td><td>{s['t'] - s['sg']}</td>"
        f"<td>{s['sg']}/{s['sa']}</td><td>{s['pen']}</td><td>{s['w']}</td><td>{s['d']}</td></tr>"
        for i, (pid, s) in enumerate(ranked, 1)]
    trow = [
        f"<tr><td>{t['pos'] or '?'}</td><td class='name'>{e(t['name'])}</td><td>{t['played'] or 0}</td>"
        f"<td>{t['won'] or 0}</td><td>{t['draw'] or 0}</td><td>{t['lost'] or 0}</td>"
        f"<td>{t['gf'] or 0}:{t['ga'] or 0}</td><td class='sortcol'>{t['pp'] or 0}:{t['pm'] or 0}</td></tr>"
        for t in lg["teams"]]
    grow = [
        f"<tr><td>{_fdate(g['date'])}</td><td class='name'>{e(tshort(g['home']))} – {e(tshort(g['guest']))}</td>"
        f"<td><b>{g['hg']}:{g['gg']}</b> <span class='tm'>({g['hh']}:{g['gh']})</span></td></tr>"
        for g in reversed(lg["games"]) if g["hg"] is not None]
    n = sum(1 for g in lg["games"] if g.get("parsed"))
    return f"""<section id="lg-{e(lg['short'])}">
<h1>{e(lg['name'])} <span class="muted">{e(lg['season'])}</span></h1>
<div class="chips"><span class="chip">{e(lg['short'])}</span><span class="chip">{e(lg['area'])}</span><span class="chip">{n} Spiele ausgewertet</span><span class="chip"><a href="#top">↑ Ligen</a></span></div>
<h2>Torschützen</h2>
<div class="tablewrap"><table><thead><tr><th>#</th><th class="name">Spieler</th><th>Sp</th><th>Tore</th><th>Ø</th><th>Feld</th><th>7m</th><th>2'</th><th>Verw.</th><th>Disq.</th></tr></thead>
<tbody>{''.join(prow) or '<tr><td colspan="10" class="name">Noch keine Spielberichte.</td></tr>'}</tbody></table></div>
<h2>Tabelle</h2>
<div class="tablewrap"><table><thead><tr><th>#</th><th class="name">Mannschaft</th><th>Sp</th><th>S</th><th>U</th><th>N</th><th>Tore</th><th>Punkte</th></tr></thead>
<tbody>{''.join(trow)}</tbody></table></div>
<h2>Ergebnisse</h2>
<div class="tablewrap"><table><thead><tr><th>Datum</th><th class="name">Spiel</th><th>Ergebnis</th></tr></thead>
<tbody>{''.join(grow)}</tbody></table></div>
</section>"""


def static_html(data: dict) -> str:
    """Pre-rendered fallback for viewers without JavaScript (e.g. iOS Mail/Files preview)."""
    from html import escape as e
    stand = datetime.fromisoformat(data["generated"]).strftime("%d.%m.%Y, %H:%M")
    leagues = data["leagues"]
    index = ""
    if len(leagues) > 1:
        index = '<div class="chips" id="top">' + "".join(
            f'<span class="chip"><a href="#lg-{e(lg["short"])}">{e(lg["name"])}</a></span>' for lg in leagues) + "</div>"
    return f"""<div class="nojs-note">Vereinfachte Ansicht (Stand: {stand}):     Diese Vorschau führt kein JavaScript aus (z.&nbsp;B. Mail- oder Dateien-App auf dem iPhone).
    Sortierung, Filter sowie Spieler-, Team- und Spielseiten gibt es, wenn die Seite in einem Browser geöffnet wird.</div>
{index}
{''.join(_static_league(lg, e) for lg in leagues)}"""


def main() -> None:
    out_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_OUT
    out_dir.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB_PATH)
    data = export(con)
    con.close()
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    html = (TEMPLATE.read_text(encoding="utf-8")
            .replace("/*__DATA__*/null", payload)
            .replace("<!--__STATIC__-->", static_html(data)))
    (out_dir / "index.html").write_text(html, encoding="utf-8")
    (out_dir / "data.json").write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    for f in STATIC.glob("*"):
        shutil.copy2(f, out_dir / f.name)
    (out_dir / "manifest.webmanifest").write_text(json.dumps({
        "name": "Handball Statistiken", "short_name": "HB Stats", "start_url": "./", "display": "standalone",
        "background_color": "#1e6fd9", "theme_color": "#1a1d22",
        "icons": [{"src": "icon-192.png", "sizes": "192x192", "type": "image/png"},
                  {"src": "icon-512.png", "sizes": "512x512", "type": "image/png"}],
    }, indent=1), encoding="utf-8")
    n_games = sum(1 for lg in data["leagues"] for g in lg["games"] if g["parsed"])
    print(f"Wrote {out_dir / 'index.html'} ({len(html) // 1024} KB, {n_games} games with reports)")


if __name__ == "__main__":
    main()
