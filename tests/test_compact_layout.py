import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
APP_SOURCE = (ROOT / "app.py").read_text(encoding="utf-8")


class ApplicationCardRedesignTests(unittest.TestCase):
    def test_card_uses_one_full_width_vertical_grid(self) -> None:
        self.assertIn(
            ".details-grid {{\n"
            "                display: grid;\n"
            "                grid-template-columns: minmax(0, 1fr);",
            APP_SOURCE,
        )
        self.assertIn('<section class="panel panel-application">', APP_SOURCE)
        self.assertNotIn('<section class="panel panel-periods">', APP_SOURCE)
        self.assertIn('<section class="panel panel-finance">', APP_SOURCE)
        self.assertIn('<section class="panel panel-photos">', APP_SOURCE)

    def test_application_has_visible_edit_action_and_all_fields(self) -> None:
        start = APP_SOURCE.index("def application_card_html(")
        end = APP_SOURCE.index("def invoice_form_html(", start)
        source = APP_SOURCE[start:end]

        self.assertIn("Изменить", source)
        for css_class in (
            "application-number",
            "application-entity",
            "application-content",
            "application-total",
            "application-sent-at",
            "application-signed-at",
        ):
            with self.subTest(css_class=css_class):
                self.assertIn(css_class, source)

    def test_application_save_is_atomic_rpc(self) -> None:
        route_start = APP_SOURCE.index('@app.put("/applications/{application_id}")')
        route_end = APP_SOURCE.index(
            "async def update_application_date(",
            route_start,
        )
        route = APP_SOURCE[route_start:route_end]

        self.assertIn('"save_application_card"', route)
        self.assertIn('"p_legal_entity_id"', route)
        self.assertIn('"p_total_sum"', route)

    def test_periods_are_compact_and_open_inside_application(self) -> None:
        card_start = APP_SOURCE.index("def application_card_html(")
        card_end = APP_SOURCE.index("def invoice_form_html(", card_start)
        card_source = APP_SOURCE[card_start:card_end]
        start = APP_SOURCE.index("def periods_html(")
        end = APP_SOURCE.index("def photo_reports_html(", start)
        source = APP_SOURCE[start:end]

        self.assertLess(
            card_source.index("overview-content"),
            card_source.index("{periods_block}"),
        )
        self.assertLess(
            card_source.index("{periods_block}"),
            card_source.index("overview-money"),
        )
        self.assertIn('class="period-view"', source)
        self.assertIn("Период размещения", source)
        self.assertIn('class="icon-action"', source)
        self.assertIn('class="period-editor hidden"', source)
        self.assertIn("savePeriods(", source)
        self.assertIn('onclick="addPeriodRow(', source)
        self.assertIn('period_value = "; ".join(', source)
        self.assertIn(
            ".period-editor.hidden {{\n"
            "                display: none;",
            APP_SOURCE,
        )


class InvoiceWorkflowTests(unittest.TestCase):
    def test_one_row_contains_every_invoice_workflow_field(self) -> None:
        start = APP_SOURCE.index("def finance_table_html(")
        end = APP_SOURCE.index("def upd_status_html(", start)
        source = APP_SOURCE[start:end]

        for label in (
            "Счёт",
            "Сумма",
            "Срок оплаты",
            "Оплата",
            "Дата отгрузки",
            "УПД",
        ):
            with self.subTest(label=label):
                self.assertIn(f"<span>{label}</span>", source)

        self.assertIn('class="finance-row workflow-row"', source)
        self.assertIn("invoice_form_html(", source)

    def test_invoice_editor_has_only_approved_user_fields(self) -> None:
        start = APP_SOURCE.index("def invoice_form_html(")
        end = APP_SOURCE.index("def payment_form_html(", start)
        source = APP_SOURCE[start:end]

        for css_class in (
            "invoice-number",
            "invoice-amount",
            "invoice-due-date",
            "invoice-shipment-date",
            "invoice-paid",
            "invoice-upd-status",
        ):
            with self.subTest(css_class=css_class):
                self.assertIn(css_class, source)

        for removed_class in (
            "invoice-content",
            "invoice-service-start",
            "invoice-service-end",
            "payment-amount",
            "payment-date",
        ):
            with self.subTest(removed_class=removed_class):
                self.assertNotIn(removed_class, source)

    def test_payment_and_upd_are_not_separate_interface_sections(self) -> None:
        dashboard_start = APP_SOURCE.index("@app.get(\"/\", response_class=HTMLResponse)")
        dashboard_end = APP_SOURCE.index('@app.get("/health")', dashboard_start)
        dashboard = APP_SOURCE[dashboard_start:dashboard_end]

        self.assertNotIn("<h3>Платежи</h3>", dashboard)
        self.assertNotIn("<h3>УПД</h3>", dashboard)
        self.assertNotIn("panel-upds", dashboard)
        self.assertNotIn("Фактические оплаты", dashboard)
        self.assertNotIn(">Остаток<", dashboard)
        self.assertNotIn(">Долг<", dashboard)

    def test_payment_status_is_derived_from_checkbox_and_due_date(self) -> None:
        start = APP_SOURCE.index("def finance_table_html(")
        end = APP_SOURCE.index("def upd_status_html(", start)
        source = APP_SOURCE[start:end]

        self.assertIn('invoice.get("is_paid")', source)
        self.assertIn("due_date < today", source)
        self.assertIn("Ожидает оплаты", source)
        self.assertIn("Просрочен", source)
        self.assertIn("Оплачен", source)
        self.assertNotIn("paid_amount", source)
        self.assertNotIn("payment_date", source)

    def test_upd_has_exactly_four_manual_statuses(self) -> None:
        start = APP_SOURCE.index("def invoice_form_html(")
        end = APP_SOURCE.index("def payment_form_html(", start)
        source = APP_SOURCE[start:end]

        expected = (
            ("waiting", "Ожидает"),
            ("formed", "Сформирован"),
            ("sent", "Отправлен"),
            ("signed", "Подписан"),
        )

        for value, label in expected:
            with self.subTest(value=value):
                self.assertIn(f'("{value}", "{label}")', source)

        self.assertNotIn("Номер УПД", source)
        self.assertNotIn("Дата УПД", source)

    def test_waiting_upd_gets_reminder_after_shipment_date(self) -> None:
        start = APP_SOURCE.index("def finance_table_html(")
        end = APP_SOURCE.index("def upd_status_html(", start)
        source = APP_SOURCE[start:end]

        self.assertIn('upd_status == "waiting"', source)
        self.assertIn("shipment_date <= today", source)
        self.assertIn("Пора сформировать", source)
        self.assertNotIn('upd_status = "formed"', source)

    def test_invoice_save_uses_single_workflow_rpc(self) -> None:
        start = APP_SOURCE.index(
            '@app.put(\n'
            '    "/applications/{application_id}/invoices/{invoice_id}"'
        )
        end = APP_SOURCE.index(
            "@app.delete(",
            start,
        )
        source = APP_SOURCE[start:end]

        self.assertIn('"save_invoice_workflow"', source)
        self.assertIn('"p_is_paid"', source)
        self.assertIn('"p_upd_status"', source)

    def test_invoice_creation_is_atomic_with_upd_status(self) -> None:
        start = APP_SOURCE.index(
            '@app.post("/applications/{application_id}/invoices")'
        )
        end = APP_SOURCE.index(
            '@app.put(\n'
            '    "/applications/{application_id}/invoices/{invoice_id}"',
            start,
        )
        source = APP_SOURCE[start:end]

        self.assertIn('"create_invoice_workflow"', source)
        self.assertIn('"p_is_paid"', source)
        self.assertIn('"p_upd_status"', source)
        self.assertNotIn('.table("payments")', source)

    def test_next_invoice_number_is_prefilled_and_editable(self) -> None:
        self.assertIn("suggest_next_invoice_number(invoices)", APP_SOURCE)
        self.assertIn('class="invoice-number"', APP_SOURCE)
        self.assertIn("suggested_invoice_no", APP_SOURCE)


class PhotoReportPresentationTests(unittest.TestCase):
    def test_month_contains_link_when_photo_exists(self) -> None:
        start = APP_SOURCE.index("def photo_reports_html(")
        end = APP_SOURCE.index("# Страница", start)
        source = APP_SOURCE[start:end]

        self.assertIn('<a class="photo-period photo-complete"', source)
        self.assertIn('href="{safe_url}"', source)
        self.assertIn("target=\"_blank\"", source)

    def test_photo_states_keep_agreed_visual_logic(self) -> None:
        start = APP_SOURCE.index("def photo_reports_html(")
        end = APP_SOURCE.index("# Страница", start)
        source = APP_SOURCE[start:end]

        self.assertIn("photo-not-required", source)
        self.assertIn("photo-overdue photo-blink", source)
        self.assertIn(
            "photo_report_is_overdue(report, today)",
            source,
        )
        self.assertIn(
            "f'<span class=\"photo-period\">{label}</span>'",
            source,
        )

    def test_photo_editor_has_no_visible_comment_field(self) -> None:
        start = APP_SOURCE.index("def photo_reports_html(")
        end = APP_SOURCE.index("# Страница", start)
        source = APP_SOURCE[start:end]

        self.assertNotIn(">Комментарий<", source)
        self.assertNotIn("photo-comment-view", source)
        self.assertIn('type="hidden"', source)
        self.assertIn('class="photo-comment"', source)
        self.assertIn("Изменить фотоотчёт", source)


class PhotoReportFilterRemovalTests(unittest.TestCase):
    def test_dashboard_keeps_only_approved_filter_groups(self) -> None:
        start = APP_SOURCE.index('@app.get("/", response_class=HTMLResponse)')
        end = APP_SOURCE.index('@app.get("/health")', start)
        source = APP_SOURCE[start:end]

        for title in ("Юридическое лицо", "Месяц фотоотчёта", "Статус"):
            with self.subTest(title=title):
                self.assertIn(f'<div class="filter-title">{title}</div>', source)

        self.assertEqual(source.count('class="filter-title"'), 3)
        self.assertNotIn('<div class="filter-title">Фотоотчёт</div>', source)
        self.assertNotIn("photo_state", source)
        self.assertNotIn("НЕ ТРЕБУЕТСЯ", source)

    def test_remaining_filtering_and_three_column_layout_stay_in_place(self) -> None:
        start = APP_SOURCE.index('@app.get("/", response_class=HTMLResponse)')
        end = APP_SOURCE.index('@app.get("/health")', start)
        source = APP_SOURCE[start:end]

        self.assertIn('if entity and item["entity_name"] != entity:', source)
        self.assertIn("if status and app_status != status:", source)
        self.assertIn("if month:", source)
        self.assertIn('for report in item["photo_reports"]', source)
        self.assertIn("grid-template-columns: 1fr 1.4fr 1.1fr;", source)
        self.assertIn('<a class="reset-link" href="/">', source)


if __name__ == "__main__":
    unittest.main()
