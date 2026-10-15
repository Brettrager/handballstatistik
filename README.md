# Handballstatistik (Prototyp)

Replacement for handballstatistiken.de for the 2026/27 season. Pulls the schedule, table and
official match reports (PDF "Presseinformation - lang") from handball4all, parses them into a
SQLite database and generates a self-contained static website.

## Usage

```
pip install -r requirements.txt
update.bat              # incremental update (only new match reports are downloaded)
update.bat --reparse    # re-parse all reports from the PDFs (after parser changes)
update.bat --refresh    # re-download + re-parse all reports
```

Output: `04_Deliverables/Handballstatistiken/index.html` (open directly in the browser; all
data is embedded) plus `data.json`, the app icons and `manifest.webmanifest`.

### How updates stay cheap

Match reports never change after the game, so every parsed report is cached as
`data/parsed/{sGID}.json` (~18 KB each). Each run only calls the handball4all API once per league
(schedule + table), downloads and parses reports for **new** games only, and rebuilds
`data/handball.db` from the API data + the cache (the DB is a derived file and is not versioned).
Player IDs are a hash of team + name, so links stay stable across rebuilds.

## Hosting (GitHub Pages, free)

The folder is set up as a GitHub repository: `.github/workflows/update.yml` runs every Monday at
05:00 UTC (and on demand via **Actions → Update & deploy → Run workflow**). It scrapes new reports,
commits the new `data/parsed/*.json` files, builds the site and publishes it to GitHub Pages
(`https://<user>.github.io/<repo>/`).

One-time setup: public repo (required for free Pages) → upload this folder (without `data/`) →
**Settings → Pages → Source: GitHub Actions** → run the workflow once (first run downloads all
reports, a few minutes).

Notes:
- GitHub disables scheduled workflows after 60 days without repository activity (summer break).
  Re-enable under **Actions** with one click.
- If a league fails to update, the other leagues are still published and the run is marked failed
  (GitHub sends an e-mail).
- New season: update `season` and the `lid` of the individual leagues (M-LL-2-BW, mB-OL-2-BW,
  mB-BOL-1-SRM) in `leagues.json`; district leagues are discovered automatically.

Viewers that don't run JavaScript (e.g. the iOS Mail/Files preview) get a pre-rendered,
read-only fallback (scorers, standings, results). The interactive app replaces it when
JavaScript is available.

## Files

| File | Purpose |
|---|---|
| `leagues.json` | Districts/leagues to track (see below) |
| `scrape.py` | Calls the handball4all JSON API, downloads + parses reports, writes `data/handball.db` |
| `parse_report.py` | PDF parser (header, both rosters, play-by-play) incl. cross-validation |
| `build_site.py` | Exports the DB to JSON and injects it into `site_template.html` |
| `site_template.html` | The single-page web app (vanilla JS, no dependencies) |
| `static/` | App icons (copied into the site) |
| `data/parsed/` | Parsed report cache (by `sGID`), versioned |
| `data/reports/` | Downloaded report PDFs (local only, not versioned) |

## Leagues

`leagues.json` supports two ways of selecting leagues:

```json
{
  "season": "2026/27",
  "districts": [
    {"og": 216, "o": 251, "area": "Stuttgart-Rems-Murr",
     "include": ["M-", "F-"], "exclude": ["M-Pok", "F-Pok"]}
  ],
  "leagues": [{"og": 216, "lid": 161166, "area": "Baden-Württemberg"}]
}
```

- `districts`: all leagues of a district (`o` = district ID, see the `oId` in the handball4all
  portal URL) are discovered automatically each run and filtered by short-name prefix
  (case-sensitive; currently all men's/women's leagues except the cups, i.e. no youth or M40).
  League IDs change every season, so this keeps working without edits.
- `leagues`: individual leagues (`lid` from `.../league?ogId=216&lId=167301`), e.g. M-LL-2-BW and the
  youth league mB-OL-2-BW. Youth leagues get their own "Jugend" row in the navigation; the game length
  (40/50/60 min) is detected from the reports, so charts adapt automatically.

Leagues that are no longer configured are removed from the database on the next run.

## Site navigation

- League bar (Männer / Frauen / Jugend) in the header; a ★ marks leagues that contain a favourite team.
- URLs carry the league, e.g. `#/M-BL-2-SRM/tabelle`, so links can be shared. Team, player and game
  links (`#/team/…`, `#/player/…`, `#/game/…`) switch to the right league automatically.
- The search box finds players and teams across all leagues.

## Data notes

- Text is extracted via PyMuPDF `get_texttrace()` because the PDFs' Unicode maps are broken for
  umlauts (normal text extraction yields `�`).
- Per-player numbers come from the roster table; the play-by-play log is used for the timeline,
  minute-based charts and runs. Both are cross-checked (goals vs. final score, goals and 7m attempts
  per player vs. log); mismatches are printed by `scrape.py` and stored in `report_warnings`.
- Players are identified by name + team. Referee names are anonymised ("N.N.") in public reports.
- Some reports log goals without a known player (`Tor durch` or `Tor durch Spieler 8, Team`). The
  scoring team is inferred from the score change; these goals count for the team but no player and
  are shown as "ohne Spielerzuordnung" in the box score.
- Tied teams have an empty rank in the API; they get the rank of the team above (as on handball4all).
- A player counts as having played a game when listed in the report roster (same as the original site).
