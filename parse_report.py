"""Parse handball4all "Presseinformation - lang" match report PDFs.

Text is read via PyMuPDF's texttrace because the PDFs' ToUnicode maps are broken
for non-ASCII glyphs (get_text() returns U+FFFD for umlauts), while the raw CIDs
carry the correct Unicode code points.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field, asdict

import pymupdf

OFFICIAL_CODES = {"A", "B", "C", "D", "E"}
ROSTER_COLUMNS = ["goals", "sevenm", "warning", "pen1", "pen2", "pen3", "disq", "report", "total_pen"]


@dataclass
class Span:
    x0: float
    x1: float
    y: float
    text: str

    @property
    def xc(self) -> float:
        return (self.x0 + self.x1) / 2


@dataclass
class RosterEntry:
    code: str
    name: str
    official: bool
    number_changes: str = ""
    goals: int = 0
    sevenm_attempts: int = 0
    sevenm_goals: int = 0
    warning: str = ""
    penalties: list = field(default_factory=list)
    disq: str = ""
    report: str = ""


@dataclass
class Event:
    clock: str
    game_time: str
    minute: int
    score_home: int | None
    score_guest: int | None
    type: str
    side: str | None
    player: str | None
    number: str | None
    text: str


def _spans_by_page(doc) -> list[list[Span]]:
    pages = []
    for page in doc:
        spans = []
        for t in page.get_texttrace():
            chars = t["chars"]
            if not chars:
                continue
            text = "".join(chr(c[0]) if c[0] > 0 else "?" for c in chars)
            x0 = min(c[3][0] for c in chars)
            x1 = max(c[3][2] for c in chars)
            y = round(chars[0][2][1], 1)
            spans.append(Span(x0, x1, y, text))
        pages.append(spans)
    return pages


def _rows(spans: list[Span]) -> list[list[Span]]:
    rows: dict[float, list[Span]] = {}
    for s in spans:
        key = next((k for k in rows if abs(k - s.y) < 1.5), s.y)
        rows.setdefault(key, []).append(s)
    return [sorted(rows[k], key=lambda s: s.x0) for k in sorted(rows)]


def _row_text(row: list[Span]) -> str:
    return " ".join(s.text.strip() for s in row if s.text.strip())


def _is_footer(text: str) -> bool:
    return text.startswith(("Handball4all AG", "Presseinformation", "Seite ")) or bool(
        re.match(r"^[A-Z0-9-]+, Spiel Nr\.", text)
    )


def _to_int(v: str) -> int:
    v = v.strip()
    return int(v) if v.isdigit() else 0


def _parse_header(lines: list[str]) -> dict:
    info: dict = {}
    joined = "\n".join(lines)
    if m := re.search(r"Spiel/Datum\s+(\d+)\s*,\s*am\s+(\d\d\.\d\d\.\d\d)\s+um\s+(\d\d:\d\d)", joined):
        info["game_no"], info["date"], info["time"] = m.groups()
    if m := re.search(r"Spielort\s+(.+)", joined):
        info["venue"] = m.group(1).strip()
    if m := re.search(r"Endstand\s+(\d+):(\d+)\s*\((\d+):(\d+)\)", joined):
        info["final"] = [int(m.group(1)), int(m.group(2))]
        info["half"] = [int(m.group(3)), int(m.group(4))]
    if m := re.search(r"Zuschauer:\s*(\S+)", joined):
        info["spectators"] = int(m.group(1)) if m.group(1).isdigit() else None
    if m := re.search(r"Spielklasse\s+(.+)", joined):
        info["league"] = m.group(1).strip()
    return info


def _assign_columns(header_spans: list[Span]) -> dict[str, float]:
    """Map roster column keys to x-centres using the 3-line table header."""
    cols: dict[str, float] = {}
    by_text = {}
    for s in header_spans:
        by_text.setdefault(s.text.strip(), []).append(s)

    def first(t):
        return by_text[t][0].xc if t in by_text else None

    cols["goals"] = first("(ges)")
    cols["sevenm"] = by_text["Tore"][-1].xc if len(by_text.get("Tore", [])) > 1 else first("7m/")
    cols["warning"] = first("Verw.")
    cols["pen1"], cols["pen2"], cols["pen3"] = first("1."), first("2."), first("3.")
    cols["disq"] = first("Disq.")
    cols["report"] = first("Ber.")
    cols["total_pen"] = first("Strafe")
    return {k: v for k, v in cols.items() if v is not None}


def _parse_roster_row(row: list[Span], cols: dict[str, float]) -> RosterEntry | None:
    code_span = row[0]
    if code_span.x0 > 75:
        return None
    code = code_span.text.strip()
    name_spans = [s for s in row[1:] if s.x0 < 200]
    raw_name = " ".join(s.text.strip() for s in name_spans).strip()
    if not raw_name or raw_name.startswith("N.N."):
        return None
    changes = ""
    if m := re.search(r"\((\d+\s*->\s*\d+)\)", raw_name):
        changes = m.group(1).replace(" ", "")
        raw_name = raw_name[: m.start()].strip()
    entry = RosterEntry(code=code, name=re.sub(r"\s+", " ", raw_name), official=code in OFFICIAL_CODES,
                        number_changes=changes)
    for s in row[1:]:
        if s.x0 < 255:
            continue
        val = s.text.strip()
        col = min(cols, key=lambda k: abs(cols[k] - s.xc))
        if col == "goals":
            entry.goals = _to_int(val)
        elif col == "sevenm" and "/" in val:
            a, g = val.split("/", 1)
            entry.sevenm_attempts, entry.sevenm_goals = _to_int(a), _to_int(g)
        elif col == "warning":
            entry.warning = val
        elif col in ("pen1", "pen2", "pen3"):
            entry.penalties.append(val)
        elif col == "disq":
            entry.disq = val
        elif col == "report":
            entry.report = val
    return entry


EVENT_RE = re.compile(r"^(?P<clock>\d\d:\d\d:\d\d)\s+(?P<gt>\d\d:\d\d)\s+(?:(?P<sh>\d+):(?P<sg>\d+)\s+)?(?P<action>.+)$")
PLAYER_RE = re.compile(r"(?P<name>.+?)\s+\((?P<nr>[0-9A-E]+),\s*(?P<team>.+)\)(?P<rest>.*)$")
EVENT_TYPES = [
    ("7m, KEIN Tor durch ", "7m_miss"),
    ("7m-Tor durch ", "7m_goal"),
    ("Tor durch ", "goal"),
    ("2-min Strafe für ", "2min"),
    ("Verwarnung für ", "warning"),
    ("Disqualifikation für ", "disq"),
    ("Trikotwechsel ", "number_change"),
    ("Auszeit ", "timeout"),
]


def _parse_event(line: str, home_names: set[str], guest_names: set[str], home_full: str, guest_full: str,
                 home_short: str, guest_short: str) -> Event | None:
    m = EVENT_RE.match(line)
    if not m:
        return None
    action = m.group("action").strip()
    gt = m.group("gt")
    ev = Event(clock=m.group("clock"), game_time=gt, minute=int(gt.split(":")[0]),
               score_home=int(m.group("sh")) if m.group("sh") else None,
               score_guest=int(m.group("sg")) if m.group("sg") else None,
               type="other", side=None, player=None, number=None, text=action)
    rest = action
    for prefix, typ in EVENT_TYPES:
        if action.startswith(prefix) or action == prefix.strip():
            ev.type = typ
            rest = action[len(prefix):]
            break
    if ev.type == "timeout":
        ev.side = "home" if rest.strip() in (home_full, home_short) else "guest" if rest.strip() in (guest_full, guest_short) else None
        return ev
    if pm := PLAYER_RE.match(rest):
        ev.player, ev.number = pm.group("name").strip(), pm.group("nr")
        team = pm.group("team").strip()
        if team in (home_full, home_short):
            ev.side = "home"
        elif team in (guest_full, guest_short):
            ev.side = "guest"
        elif ev.player in home_names and ev.player not in guest_names:
            ev.side = "home"
        elif ev.player in guest_names and ev.player not in home_names:
            ev.side = "guest"
    return ev


def parse_report(path: str, home_short: str = "", guest_short: str = "") -> dict:
    doc = pymupdf.open(path)
    pages = _spans_by_page(doc)

    header_lines: list[str] = []
    rosters: dict[str, dict] = {"home": {"team": "", "entries": []}, "guest": {"team": "", "entries": []}}
    log_lines: list[str] = []
    section = "header"
    side = None
    header_buf: list[Span] = []
    cols: dict[str, float] = {}

    for spans in pages:
        for row in _rows(spans):
            text = _row_text(row)
            if not text or _is_footer(text):
                continue
            if text.startswith("Mannschaftslisten"):
                section = "roster"
                continue
            if text.startswith("Spielverlauf"):
                section = "log"
                continue
            if section == "header":
                header_lines.append(text)
            elif section == "roster":
                if m := re.match(r"^(Heim|Gast):\s*(.+)$", text):
                    side = "home" if m.group(1) == "Heim" else "guest"
                    rosters[side]["team"] = m.group(2).strip()
                    header_buf, cols = [], {}
                    continue
                if side is None:
                    continue
                if not cols:
                    header_buf.extend(row)
                    if any(s.text.strip() == "Strafe" for s in row):
                        cols = _assign_columns(header_buf)
                    continue
                entry = _parse_roster_row(row, cols)
                if entry:
                    rosters[side]["entries"].append(entry)
            elif section == "log":
                if text.startswith("Zeit Spielzeit"):
                    continue
                log_lines.append(text)

    info = _parse_header(header_lines)
    home_full, guest_full = rosters["home"]["team"], rosters["guest"]["team"]
    home_names = {e.name for e in rosters["home"]["entries"]}
    guest_names = {e.name for e in rosters["guest"]["entries"]}
    events = []
    for line in log_lines:
        ev = _parse_event(line, home_names, guest_names, home_full, guest_full, home_short, guest_short)
        if ev:
            events.append(ev)
    _infer_goal_sides(events)

    return {
        "info": info,
        "home": {"team": home_full, "entries": [asdict(e) for e in rosters["home"]["entries"]]},
        "guest": {"team": guest_full, "entries": [asdict(e) for e in rosters["guest"]["entries"]]},
        "events": [asdict(e) for e in events],
        "warnings": validate(info, rosters, events),
    }


def _infer_goal_sides(events: list[Event]) -> None:
    """Goals logged without a (known) player, e.g. "Tor durch" or "Tor durch Spieler 8, Team", get their
    side from the score change. Such goals stay unattributed (player=None)."""
    prev = (0, 0)
    for ev in events:
        if ev.score_home is None:
            continue
        if ev.type in ("goal", "7m_goal") and ev.side is None:
            if ev.score_home > prev[0] and ev.score_guest == prev[1]:
                ev.side = "home"
            elif ev.score_guest > prev[1] and ev.score_home == prev[0]:
                ev.side = "guest"
            ev.player = ev.number = None
        prev = (ev.score_home, ev.score_guest)


def validate(info: dict, rosters: dict, events: list[Event]) -> list[str]:
    """Cross-check roster totals against the final score and the event log."""
    warnings = []
    final = info.get("final")
    for idx, side in enumerate(("home", "guest")):
        players = [e for e in rosters[side]["entries"] if not e.official]
        total = sum(e.goals for e in players)
        unattributed = sum(1 for ev in events if ev.side == side and ev.player is None and ev.type in ("goal", "7m_goal"))
        if final and total + unattributed != final[idx]:
            warnings.append(f"{side}: roster goals {total} (+{unattributed} unattributed) != final score {final[idx]}")
        if events:
            for p in players:
                log_goals = sum(1 for ev in events if ev.side == side and ev.player == p.name and ev.type in ("goal", "7m_goal"))
                if log_goals != p.goals:
                    warnings.append(f"{side}: {p.name} roster goals {p.goals} != log goals {log_goals}")
                log_7m = sum(1 for ev in events if ev.side == side and ev.player == p.name and ev.type in ("7m_goal", "7m_miss"))
                if log_7m != p.sevenm_attempts:
                    warnings.append(f"{side}: {p.name} roster 7m attempts {p.sevenm_attempts} != log {log_7m}")
    return warnings


if __name__ == "__main__":
    import json
    import sys

    sys.stdout.reconfigure(encoding="utf-8")
    print(json.dumps(parse_report(sys.argv[1]), ensure_ascii=False, indent=1)[:6000])
