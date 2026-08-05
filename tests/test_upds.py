import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
APP_SOURCE = (ROOT / "app.py").read_text(encoding="utf-8")
MIGRATION = (
    ROOT
    / "supabase"
    / "migrations"
    / "stage_6_application_card_redesign.sql"
).read_text(encoding="utf-8")
CREATE_MIGRATION = (
    ROOT
    / "supabase"
    / "migrations"
    / "stage_6_atomic_invoice_creation.sql"
).read_text(encoding="utf-8")


class SimplifiedDataModelTests(unittest.TestCase):
    def test_dashboard_loads_paid_flag_and_upd_status(self) -> None:
        start = APP_SOURCE.index("def load_dashboard_data()")
        end = APP_SOURCE.index("# HTML-фрагменты", start)
        source = APP_SOURCE[start:end]

        self.assertIn('"shipment_date,is_paid"', source)
        self.assertIn('"id,invoice_id,upd_no,upd_date,sent_at,signed_at,status"', source)
        self.assertNotIn('fetch_all(\n        "payments"', source)

    def test_legacy_payment_routes_are_not_rendered(self) -> None:
        dashboard_start = APP_SOURCE.index("@app.get(\"/\", response_class=HTMLResponse)")
        dashboard_end = APP_SOURCE.index('@app.get("/health")', dashboard_start)
        dashboard = APP_SOURCE[dashboard_start:dashboard_end]

        self.assertNotIn("payment_form_html(", dashboard)
        self.assertNotIn("payment_lines_html(", dashboard)
        self.assertNotIn("payment_editors_html(", dashboard)
        self.assertNotIn("upds_html(", dashboard)

    def test_status_save_is_part_of_invoice_payload(self) -> None:
        js_start = APP_SOURCE.index("function invoicePayload(editor)")
        js_end = APP_SOURCE.index(
            "async function saveInvoice",
            js_start,
        )
        source = APP_SOURCE[js_start:js_end]

        self.assertIn(".invoice-paid", source)
        self.assertIn(".invoice-upd-status", source)
        self.assertNotIn(".payment-amount", source)
        self.assertNotIn(".upd-number", source)


class CardRedesignMigrationTests(unittest.TestCase):
    def test_migration_adds_only_non_destructive_workflow_columns(self) -> None:
        self.assertIn(
            "add column if not exists is_paid boolean not null default false",
            MIGRATION,
        )
        self.assertIn(
            "add column if not exists status text not null default 'waiting'",
            MIGRATION,
        )
        self.assertNotIn("drop table", MIGRATION.lower())
        self.assertNotIn("delete from public.payments", MIGRATION.lower())
        self.assertNotIn("delete from public.upds", MIGRATION.lower())

    def test_payment_history_backfills_only_full_payment_flag(self) -> None:
        self.assertIn("update public.invoices i", MIGRATION)
        self.assertIn("coalesce(sum(p.amount), 0) >= i.amount", MIGRATION)
        self.assertIn("from public.payments p", MIGRATION)

    def test_upd_status_backfill_preserves_existing_progress(self) -> None:
        self.assertIn(
            "when signed_at is not null then 'signed'",
            MIGRATION,
        )
        self.assertIn(
            "when sent_at is not null then 'sent'",
            MIGRATION,
        )
        self.assertIn(
            "when upd_no is not null then 'formed'",
            MIGRATION,
        )
        self.assertIn("else 'waiting'", MIGRATION)

    def test_migration_keeps_one_upd_row_per_invoice(self) -> None:
        self.assertIn("insert into public.upds", MIGRATION)
        self.assertIn("left join public.upds u on u.invoice_id = i.id", MIGRATION)
        self.assertIn("where u.id is null", MIGRATION)
        self.assertIn("having count(*) > 1", MIGRATION)

    def test_functions_are_invoker_and_service_role_only(self) -> None:
        self.assertGreaterEqual(MIGRATION.count("security invoker"), 2)
        self.assertIn(
            "grant execute on function public.save_application_card",
            MIGRATION,
        )
        self.assertIn(
            "grant execute on function public.save_invoice_workflow",
            MIGRATION,
        )
        self.assertGreaterEqual(MIGRATION.count("from anon"), 2)
        self.assertGreaterEqual(MIGRATION.count("from authenticated"), 2)

    def test_new_invoice_and_upd_are_created_atomically(self) -> None:
        self.assertIn(
            "create or replace function public.create_invoice_workflow",
            CREATE_MIGRATION,
        )
        self.assertIn("insert into public.invoices", CREATE_MIGRATION)
        self.assertIn("insert into public.upds", CREATE_MIGRATION)
        self.assertIn("security invoker", CREATE_MIGRATION)
        self.assertIn(
            "grant execute on function public.create_invoice_workflow",
            CREATE_MIGRATION,
        )


if __name__ == "__main__":
    unittest.main()
