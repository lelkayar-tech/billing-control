import datetime
import unittest

from periods_photos import (
    InputValidationError,
    build_filter_url,
    calendar_months,
    normalize_periods,
    normalize_photo_report_payload,
    normalize_photo_url,
    photo_report_is_overdue,
    photo_report_month_end,
    report_matches_state,
)


class PeriodValidationTests(unittest.TestCase):
    def test_normalizes_and_sorts_multiple_periods(self) -> None:
        periods = normalize_periods([
            {"start_date": "2027-01-10", "end_date": "2027-02-03"},
            {"start_date": "2026-12-01", "end_date": "2026-12-31"},
        ])

        self.assertEqual(
            periods,
            [
                {
                    "start_date": "2026-12-01",
                    "end_date": "2026-12-31",
                },
                {
                    "start_date": "2027-01-10",
                    "end_date": "2027-02-03",
                },
            ],
        )

    def test_rejects_empty_reversed_and_duplicate_periods(self) -> None:
        invalid_values = [
            [],
            [{"start_date": "2026-06-02", "end_date": "2026-06-01"}],
            [
                {"start_date": "2026-06-01", "end_date": "2026-06-30"},
                {"start_date": "2026-06-01", "end_date": "2026-06-30"},
            ],
        ]

        for value in invalid_values:
            with self.subTest(value=value):
                with self.assertRaises(InputValidationError):
                    normalize_periods(value)

    def test_calendar_months_deduplicates_overlaps_and_crosses_year(self) -> None:
        months = calendar_months([
            {"start_date": "2026-11-20", "end_date": "2027-01-05"},
            {"start_date": "2026-12-01", "end_date": "2026-12-31"},
        ])

        self.assertEqual(months, [(2026, 11), (2026, 12), (2027, 1)])


class PhotoReportValidationTests(unittest.TestCase):
    def test_accepts_http_and_https_urls(self) -> None:
        self.assertEqual(
            normalize_photo_url(" https://example.com/report "),
            "https://example.com/report",
        )
        self.assertEqual(
            normalize_photo_url("http://example.com"),
            "http://example.com",
        )

    def test_rejects_unsafe_or_incomplete_urls(self) -> None:
        for value in (
            "javascript:alert(1)",
            "ftp://example.com/report",
            "example.com/report",
            "https://example.com/a b",
        ):
            with self.subTest(value=value):
                with self.assertRaises(InputValidationError):
                    normalize_photo_url(value)

    def test_not_required_clears_url_and_keeps_comment(self) -> None:
        payload = normalize_photo_report_payload({
            "url": "https://example.com/report",
            "not_required": True,
            "comment": "  По договору не нужен  ",
        })

        self.assertEqual(
            payload,
            {
                "url": None,
                "not_required": True,
                "comment": "По договору не нужен",
            },
        )

    def test_report_states_are_mutually_exclusive(self) -> None:
        present = {"url": "https://example.com", "not_required": False}
        missing = {"url": None, "not_required": False}
        not_required = {"url": None, "not_required": True}

        self.assertTrue(report_matches_state(present, "present"))
        self.assertTrue(report_matches_state(missing, "missing"))
        self.assertTrue(
            report_matches_state(not_required, "not_required")
        )
        self.assertFalse(report_matches_state(missing, "present"))

    def test_report_uses_end_of_its_own_month(self) -> None:
        report = {"report_year": 2026, "report_month": 6}
        self.assertEqual(
            photo_report_month_end(report).isoformat(),
            "2026-06-30",
        )

    def test_previous_month_is_overdue_during_continuing_placement(self) -> None:
        june_report = {"report_year": 2026, "report_month": 6}
        july_report = {"report_year": 2026, "report_month": 7}
        today = datetime.date(2026, 7, 28)

        self.assertTrue(photo_report_is_overdue(june_report, today))
        self.assertFalse(photo_report_is_overdue(july_report, today))

    def test_report_month_is_not_overdue_on_its_last_day(self) -> None:
        report = {"report_year": 2026, "report_month": 2}

        self.assertFalse(
            photo_report_is_overdue(
                report,
                datetime.date(2026, 2, 28),
            )
        )


class FilterUrlTests(unittest.TestCase):
    def test_combines_filters_and_can_clear_one(self) -> None:
        current = {
            "entity": "БЦ",
            "month": "МАЙ",
            "status": "ОПЛАЧЕНО",
            "photo_state": "missing",
        }

        combined = build_filter_url(current, month="ИЮН")
        cleared = build_filter_url(current, entity=None)

        self.assertIn("entity=%D0%91%D0%A6", combined)
        self.assertIn("month=%D0%98%D0%AE%D0%9D", combined)
        self.assertIn("status=", combined)
        self.assertNotIn("entity=", cleared)
        self.assertIn("month=", cleared)


if __name__ == "__main__":
    unittest.main()
