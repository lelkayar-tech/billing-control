import unittest

from creative_history import project_history, render_history


def row(app_id, app_no, observation, *, parent="", place="place-A", layout="A", start="2026-07-01", change=""):
    return {
        "billing_application_id": str(app_id),
        "billing_app_no": app_no,
        "billing_period_end": "2026-09-30",
        "document_status": "approved",
        "observation_id": observation,
        "parent_billing_application_id": str(parent) if parent else "",
        "place_id": place,
        "place_title_in_registry": "Щит A",
        "layout_name": layout,
        "layout_asset_ids": "",
        "placement_start": start,
        "placement_end": "2026-09-30",
        "change_date": change,
        "change_kind": "replacement" if change else "",
        "actual_status": "document_only_not_confirmed",
    }


class CreativeHistoryTests(unittest.TestCase):
    def test_ds_attaches_to_parent_and_keeps_both_layouts(self):
        records = [
            row(100, "100", "original"),
            row(101, "ДС1 к 100", "replacement", parent=100, layout="Б", start="2026-08-10", change="2026-08-10"),
        ]
        history = project_history(records)
        self.assertEqual(len(history["100"]), 2)
        self.assertEqual(history["100"][1]["source_type"], "supplement")
        self.assertEqual(history["100"][1]["change_date"], "2026-08-10")
        self.assertIn("ДС1 к 100", render_history(records, 100))

    def test_different_places_are_not_merged(self):
        history = project_history([row(100, "100", "a", place="A"), row(100, "100", "b", place="B")])
        self.assertEqual(len({x["place_id"] for x in history["100"]}), 2)

    def test_html_escaped(self):
        self.assertIn("&lt;script&gt;", render_history([row(100, "100", "a", layout="<script>")], 100))

    def test_withdrawn_excluded(self):
        record = row(100, "100", "a")
        record["document_status"] = "withdrawn"
        self.assertEqual(project_history([record]), {})


if __name__ == "__main__":
    unittest.main()
