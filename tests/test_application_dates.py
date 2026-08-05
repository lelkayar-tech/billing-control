import unittest
from pathlib import Path


APP_SOURCE = (
    Path(__file__).resolve().parents[1] / "app.py"
).read_text(encoding="utf-8")


class ApplicationDatesInterfaceTests(unittest.TestCase):
    def test_sent_and_signed_dates_have_parallel_edit_controls(self) -> None:
        expected_fragments = (
            'title="{action_label}"',
            'input_class="application-sent-at"',
            'input_class="application-signed-at"',
            'save_function="saveApplicationSentAt"',
            'save_function="saveApplicationSignedAt"',
            "function saveApplicationSentAt(",
            "function saveApplicationSignedAt(",
        )

        for fragment in expected_fragments:
            with self.subTest(fragment=fragment):
                self.assertIn(fragment, APP_SOURCE)

    def test_application_date_update_is_filtered_and_verified(self) -> None:
        helper_start = APP_SOURCE.index(
            "async def update_application_date("
        )
        helper_end = APP_SOURCE.index(
            '@app.put("/applications/{application_id}/sent-at")',
            helper_start,
        )
        helper = APP_SOURCE[helper_start:helper_end]

        self.assertIn('.table("applications")', helper)
        self.assertIn(".update(payload)", helper)
        self.assertGreaterEqual(
            helper.count('.eq("id", application_id)'),
            2,
        )
        self.assertIn('.select(f"id,{field_name}")', helper)

    def test_sent_and_signed_routes_use_correct_fields(self) -> None:
        routes_start = APP_SOURCE.index(
            '@app.put("/applications/{application_id}/sent-at")'
        )
        routes_end = APP_SOURCE.index(
            '@app.post("/applications/{application_id}/invoices")',
            routes_start,
        )
        routes = APP_SOURCE[routes_start:routes_end]

        self.assertIn('field_name="sent_at"', routes)
        self.assertIn('field_name="signed_at"', routes)
        self.assertIn("normalize_application_sent_at_payload", APP_SOURCE)
        self.assertIn("normalize_application_signed_at_payload", APP_SOURCE)

    def test_client_mode_does_not_render_date_editors(self) -> None:
        function_start = APP_SOURCE.index(
            "def application_date_html("
        )
        function_end = APP_SOURCE.index(
            "def application_sent_at_html(",
            function_start,
        )
        function = APP_SOURCE[function_start:function_end]

        self.assertIn("if IS_EDIT_MODE:", function)
        self.assertIn("edit_button = \"\"", function)
        self.assertIn("editor = \"\"", function)

    def test_sent_and_signed_date_messages_have_unique_ids(self) -> None:
        self.assertIn(
            '"application-date-message-sent-at-" + applicationId',
            APP_SOURCE,
        )
        self.assertIn(
            '"application-date-message-signed-at-" + applicationId',
            APP_SOURCE,
        )


if __name__ == "__main__":
    unittest.main()
