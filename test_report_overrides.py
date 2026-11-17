import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import scrape
from parse_report import Span, _parse_roster_row, parse_report


class ReportOverridesTest(unittest.TestCase):
    def test_anonymous_roster_identity(self):
        row = [Span(40, 50, 100, "6"), Span(80, 150, 100, "N.N. N.N."),
               Span(280, 290, 100, "10"), Span(320, 340, 100, "6/6")]
        cols = {"goals": 285, "sevenm": 330}
        self.assertIsNone(_parse_roster_row(row, cols))
        entry = _parse_roster_row(row, cols, {"6": "Rahel Baumann"})
        self.assertEqual((entry.name, entry.code, entry.goals, entry.sevenm_goals),
                         ("Rahel Baumann", "6", 10, 6))

    def test_cache_invalidates_only_when_override_changes(self):
        game = {"sGID": "123", "gHomeTeam": "Home", "gGuestTeam": "Guest"}
        override = {"guest": {"6": "Rahel Baumann"}}
        with tempfile.TemporaryDirectory() as directory:
            cache = Path(directory) / "123.json"
            cache.write_text('{"events":[]}', encoding="utf-8")
            with patch.object(scrape, "PARSED", Path(directory)), \
                    patch.object(scrape, "download_report", return_value=Path("report.pdf")) as download, \
                    patch.object(scrape, "parse_report", side_effect=lambda *args: {"events": []}) as parse:
                scrape.get_report(game, "incremental", override)
                self.assertEqual(json.loads(cache.read_text())["player_overrides"], override)
                scrape.get_report(game, "incremental", override)
                self.assertEqual(download.call_count, 1)
                self.assertEqual(parse.call_count, 1)
                scrape.get_report(game, "reparse", override)
                scrape.get_report(game, "refresh", override)
                self.assertEqual(parse.call_count, 3)
                self.assertTrue(download.call_args.kwargs["refresh"])
                scrape.get_report(game, "incremental")
                self.assertNotIn("player_overrides", json.loads(cache.read_text()))

    def test_roster_and_anonymous_timeline(self):
        pages = [[
            Span(0, 100, 10, "Endstand 0:1 (0:0)"),
            Span(0, 100, 20, "Mannschaftslisten"),
            Span(0, 100, 30, "Gast: Guest"),
            Span(280, 290, 40, "(ges)"), Span(320, 340, 40, "7m/"),
            Span(400, 420, 40, "Strafe"),
            Span(40, 50, 50, "6"), Span(80, 150, 50, "N.N. N.N."),
            Span(280, 290, 50, "1"),
            Span(0, 100, 60, "Spielverlauf"),
            Span(0, 500, 70, "12:00:00 01:00 0:1 Tor durch Spieler 6, Guest"),
        ]]
        with patch("parse_report.pymupdf.open"), patch("parse_report._spans_by_page", return_value=pages):
            report = parse_report("unused.pdf", "Home", "Guest", {"guest": {"6": "Rahel Baumann"}})
        event = report["events"][0]
        self.assertEqual((event["player"], event["number"], event["side"]),
                         ("Rahel Baumann", "6", "guest"))
        self.assertEqual(report["warnings"], [])


if __name__ == "__main__":
    unittest.main()
