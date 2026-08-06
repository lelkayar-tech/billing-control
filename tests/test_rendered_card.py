import datetime
import importlib.util
import os
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]


def load_app_module():
    os.environ["SUPABASE_URL"] = "https://example.supabase.co"
    os.environ["SUPABASE_SECRET_KEY"] = "test-key"
    os.environ["APP_MODE"] = "edit"
    spec = importlib.util.spec_from_file_location(
        "billing_control_render_test",
        ROOT / "app.py",
    )
    module = importlib.util.module_from_spec(spec)

    class DummyFastAPI:
        def __init__(self, *_args, **_kwargs):
            pass

        def __getattr__(self, _name):
            def route(*_args, **_kwargs):
                def decorator(function):
                    return function

                return decorator

            return route

    class DummyResponse:
        def __init__(self, content=None, **_kwargs):
            self.content = content

    fake_dotenv = types.ModuleType("dotenv")
    fake_dotenv.load_dotenv = lambda: None
    fake_fastapi = types.ModuleType("fastapi")
    fake_fastapi.FastAPI = DummyFastAPI
    fake_fastapi.Query = lambda default=None, **_kwargs: default
    fake_fastapi.Request = object
    fake_responses = types.ModuleType("fastapi.responses")
    fake_responses.HTMLResponse = DummyResponse
    fake_responses.JSONResponse = DummyResponse
    fake_responses.RedirectResponse = DummyResponse
    fake_supabase = types.ModuleType("supabase")
    fake_supabase.create_client = lambda *_args, **_kwargs: object()

    with patch.dict(
        sys.modules,
        {
            "dotenv": fake_dotenv,
            "fastapi": fake_fastapi,
            "fastapi.responses": fake_responses,
            "supabase": fake_supabase,
        },
    ):
        spec.loader.exec_module(module)

    return module


class RenderedCardTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = load_app_module()
        cls.application = {
            "id": 1,
            "legal_entity_id": 7,
            "app_no": "395",
            "content": "Наружная реклама",
            "total_sum": "363987",
            "sent_at": "2026-07-22",
            "signed_at": None,
        }
        cls.invoice = {
            "id": 10,
            "invoice_no": "ОМ-13290/1",
            "amount": "363987",
            "due_date": "2026-07-01",
            "shipment_date": "2026-07-20",
            "is_paid": False,
        }

    def test_edit_mode_renders_real_application_controls(self) -> None:
        result = self.app.application_card_html(
            {
                **self.application,
                "_periods": [
                    {
                        "start_date": "2026-07-01",
                        "end_date": "2026-07-31",
                    },
                    {
                        "start_date": "2026-09-01",
                        "end_date": "2026-09-30",
                    },
                ],
            },
            [{"id": 7, "name": "БЦ"}],
            "БЦ",
        )

        self.assertIn("application-editor-1", result)
        self.assertIn('class="application-number"', result)
        self.assertIn('class="application-entity"', result)
        self.assertIn("saveApplicationCard(1)", result)
        self.assertIn(
            "01.07.2026 — 31.07.2026; 01.09.2026 — 30.09.2026",
            result,
        )
        self.assertIn('class="period-editor hidden"', result)
        self.assertIn("savePeriods(1)", result)

    def test_invoice_row_renders_statuses_and_editor(self) -> None:
        result = self.app.finance_table_html(
            self.application,
            [self.invoice],
            {10: {"invoice_id": 10, "status": "waiting"}},
            datetime.date(2026, 7, 28),
        )

        self.assertIn("ОМ-13290/1", result)
        self.assertIn("Просрочен", result)
        self.assertIn("Сформирован", result)
        self.assertIn('class="invoice-paid"', result)
        self.assertIn('class="invoice-upd-status"', result)
        self.assertNotIn("Фактические оплаты", result)
        self.assertNotIn("Номер УПД", result)

    def test_photo_months_render_all_agreed_states(self) -> None:
        reports = [
            {
                "id": 1,
                "report_year": 2026,
                "report_month": 4,
                "url": "https://example.com/april",
                "not_required": False,
                "comment": "Архивный комментарий",
            },
            {
                "id": 2,
                "report_year": 2026,
                "report_month": 5,
                "url": None,
                "not_required": True,
                "comment": None,
            },
            {
                "id": 3,
                "report_year": 2026,
                "report_month": 6,
                "url": None,
                "not_required": False,
                "comment": None,
            },
            {
                "id": 4,
                "report_year": 2026,
                "report_month": 7,
                "url": None,
                "not_required": False,
                "comment": None,
            },
        ]

        result = self.app.photo_reports_html(
            1,
            reports,
            datetime.date(2026, 7, 28),
        )

        self.assertIn(
            'href="https://example.com/april"',
            result,
        )
        self.assertIn("photo-not-required", result)
        self.assertIn("photo-overdue photo-blink", result)
        self.assertIn("photo-record-overdue", result)
        self.assertIn(
            '<span class="photo-period">ИЮЛ 2026</span>',
            result,
        )
        self.assertNotIn(">Комментарий<", result)
        self.assertNotIn("Архивный комментарий</span>", result)

    def test_read_mode_renders_no_edit_controls(self) -> None:
        previous = self.app.IS_EDIT_MODE
        self.app.IS_EDIT_MODE = False

        try:
            application_html = self.app.application_card_html(
                self.application,
                [{"id": 7, "name": "БЦ"}],
                "БЦ",
            )
            invoice_html = self.app.finance_table_html(
                self.application,
                [self.invoice],
                {10: {"invoice_id": 10, "status": "waiting"}},
                datetime.date(2026, 7, 28),
            )
        finally:
            self.app.IS_EDIT_MODE = previous

        self.assertNotIn("application-editor-1", application_html)
        self.assertNotIn("invoice-form-1-10", invoice_html)
        self.assertNotIn("deleteInvoice(", invoice_html)

    def test_overdue_photo_filter_is_edit_only(self) -> None:
        previous = self.app.IS_EDIT_MODE

        try:
            with patch.object(self.app, "load_dashboard_data", return_value=[]):
                self.app.IS_EDIT_MODE = True
                edit_html = self.app.dashboard().content
                self.app.IS_EDIT_MODE = False
                read_html = self.app.dashboard().content
                legacy_url_html = self.app.dashboard(photo_overdue="1").content
        finally:
            self.app.IS_EDIT_MODE = previous

        self.assertIn("ПРОСРОЧЕН ФО", edit_html)
        self.assertNotIn("ПРОСРОЧЕН ФО", read_html)
        self.assertNotIn("ПРОСРОЧЕН ФО", legacy_url_html)
        self.assertNotIn("photo_overdue=1", legacy_url_html)

    def test_overdue_photo_parameter_filters_only_in_edit_mode(self) -> None:
        previous = self.app.IS_EDIT_MODE
        rows = [self.dashboard_row(1, "OVERDUE"), self.dashboard_row(2, "CURRENT")]

        try:
            with (
                patch.object(self.app, "load_dashboard_data", return_value=rows),
                patch.object(
                    self.app,
                    "application_has_overdue_photo_report",
                    side_effect=lambda reports, _today: reports[0]["overdue"],
                ),
                patch.object(self.app, "application_card_html", return_value=""),
                patch.object(self.app, "finance_table_html", return_value=""),
                patch.object(self.app, "photo_reports_html", return_value=""),
            ):
                self.app.IS_EDIT_MODE = True
                edit_html = self.app.dashboard(photo_overdue="1").content
                self.app.IS_EDIT_MODE = False
                read_html = self.app.dashboard(photo_overdue="1").content
        finally:
            self.app.IS_EDIT_MODE = previous

        self.assertIn("OVERDUE", edit_html)
        self.assertNotIn("CURRENT", edit_html)
        self.assertIn("OVERDUE", read_html)
        self.assertIn("CURRENT", read_html)

    @staticmethod
    def dashboard_row(application_id: int, app_no: str) -> dict:
        return {
            "application": {
                "id": application_id,
                "app_no": app_no,
                "content": app_no,
                "total_sum": "0",
                "sent_at": None,
                "signed_at": None,
            },
            "invoices": [],
            "periods": [],
            "photo_reports": [{"overdue": app_no == "OVERDUE"}],
            "entity_name": "Тест",
            "legal_entities": [],
            "upds_by_invoice": {},
        }


if __name__ == "__main__":
    unittest.main()
