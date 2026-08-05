import unittest
from pathlib import Path


APP_SOURCE = (
    Path(__file__).resolve().parents[1] / "app.py"
).read_text(encoding="utf-8")


class OpenApplicationAfterReloadTests(unittest.TestCase):
    def test_reload_is_centralized_in_state_preserving_helper(self) -> None:
        self.assertEqual(APP_SOURCE.count("window.location.reload();"), 1)
        self.assertIn(
            "function reloadWithOpenApplication(",
            APP_SOURCE,
        )
        self.assertIn(
            'const RELOAD_STATE_KEY = "billing-control-reload-state";',
            APP_SOURCE,
        )

    def test_period_and_photo_mutations_restore_open_application(self) -> None:
        self.assertGreaterEqual(
            APP_SOURCE.count("reloadWithOpenApplication("),
            4,
        )
        self.assertIn(
            'document.addEventListener(\n'
            '                "DOMContentLoaded",\n'
            "                restoreAfterReload",
            APP_SOURCE,
        )
        self.assertIn(
            'row.classList.remove("hidden");',
            APP_SOURCE,
        )


if __name__ == "__main__":
    unittest.main()
