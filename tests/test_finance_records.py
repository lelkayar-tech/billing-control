import datetime
import unittest

from finance_records import (
    FinanceValidationError,
    effective_upd_status,
    invoice_base_number,
    normalize_application_card_payload,
    normalize_application_sent_at_payload,
    normalize_application_signed_at_payload,
    normalize_invoice_payload,
    normalize_invoice_workflow_payload,
    normalize_payment_payload,
    normalize_upd_payload,
    suggest_next_invoice_number,
)


class EffectiveUpdStatusTests(unittest.TestCase):
    TODAY = datetime.date(2026, 8, 6)

    def test_future_shipment_date_is_waiting(self) -> None:
        self.assertEqual(
            effective_upd_status("waiting", "2026-08-07", self.TODAY),
            "waiting",
        )

    def test_shipment_date_today_is_formed(self) -> None:
        self.assertEqual(
            effective_upd_status("waiting", "2026-08-06", self.TODAY),
            "formed",
        )

    def test_past_shipment_date_is_formed(self) -> None:
        self.assertEqual(
            effective_upd_status("waiting", "2026-08-05", self.TODAY),
            "formed",
        )

    def test_missing_shipment_date_is_waiting(self) -> None:
        self.assertEqual(
            effective_upd_status("waiting", None, self.TODAY),
            "waiting",
        )

    def test_sent_manual_status_has_priority(self) -> None:
        self.assertEqual(
            effective_upd_status("sent", "2026-08-05", self.TODAY),
            "sent",
        )

    def test_signed_manual_status_has_priority(self) -> None:
        self.assertEqual(
            effective_upd_status("signed", "2026-08-05", self.TODAY),
            "signed",
        )


class ApplicationValidationTests(unittest.TestCase):
    def test_normalizes_complete_application_card(self) -> None:
        self.assertEqual(
            normalize_application_card_payload({
                "app_no": " 395 ",
                "legal_entity_id": "7",
                "content": " Наружная реклама ",
                "total_sum": " 363 987,50 ",
                "sent_at": "2026-07-22",
                "signed_at": "",
            }),
            {
                "app_no": "395",
                "legal_entity_id": 7,
                "content": "Наружная реклама",
                "total_sum": "363987.50",
                "sent_at": "2026-07-22",
                "signed_at": None,
            },
        )

    def test_rejects_invalid_application_card(self) -> None:
        invalid_payloads = (
            {
                "legal_entity_id": "",
                "content": "Размещение",
                "total_sum": "100",
            },
            {
                "legal_entity_id": 1,
                "content": "",
                "total_sum": "100",
            },
            {
                "legal_entity_id": 1,
                "content": "Размещение",
                "total_sum": "-1",
            },
        )

        for payload in invalid_payloads:
            with self.subTest(payload=payload):
                with self.assertRaises(FinanceValidationError):
                    normalize_application_card_payload(payload)

    def test_normalizes_or_clears_sent_at(self) -> None:
        self.assertEqual(
            normalize_application_sent_at_payload({
                "sent_at": "2026-07-28",
            }),
            {"sent_at": "2026-07-28"},
        )
        self.assertEqual(
            normalize_application_sent_at_payload({"sent_at": ""}),
            {"sent_at": None},
        )

    def test_rejects_invalid_sent_at(self) -> None:
        for payload in (
            {"sent_at": "28.07.2026"},
            {"sent_at": "1999-12-31"},
            {"sent_at": "2101-01-01"},
            "2026-07-28",
        ):
            with self.subTest(payload=payload):
                with self.assertRaises(FinanceValidationError):
                    normalize_application_sent_at_payload(payload)

    def test_normalizes_or_clears_signed_at(self) -> None:
        self.assertEqual(
            normalize_application_signed_at_payload({
                "signed_at": "2026-07-28",
            }),
            {"signed_at": "2026-07-28"},
        )
        self.assertEqual(
            normalize_application_signed_at_payload({"signed_at": ""}),
            {"signed_at": None},
        )

    def test_rejects_invalid_signed_at(self) -> None:
        for payload in (
            {"signed_at": "28.07.2026"},
            {"signed_at": "1999-12-31"},
            {"signed_at": "2101-01-01"},
            "2026-07-28",
        ):
            with self.subTest(payload=payload):
                with self.assertRaises(FinanceValidationError):
                    normalize_application_signed_at_payload(payload)


class InvoiceValidationTests(unittest.TestCase):
    def test_normalizes_simplified_invoice_workflow(self) -> None:
        self.assertEqual(
            normalize_invoice_workflow_payload({
                "invoice_no": " ОМ-13290/2 ",
                "amount": "66 734",
                "due_date": "2026-08-07",
                "shipment_date": "2026-08-31",
                "is_paid": True,
                "upd_status": "sent",
            }),
            {
                "invoice_no": "ОМ-13290/2",
                "amount": "66734",
                "due_date": "2026-08-07",
                "shipment_date": "2026-08-31",
                "is_paid": True,
                "upd_status": "sent",
            },
        )

    def test_rejects_invalid_payment_flag_or_upd_status(self) -> None:
        base = {
            "invoice_no": "ОМ-1",
            "amount": "100",
            "is_paid": False,
            "upd_status": "waiting",
        }

        for changes in (
            {"is_paid": "да"},
            {"upd_status": "готов"},
            {"amount": "0"},
        ):
            with self.subTest(changes=changes):
                payload = {**base, **changes}

                with self.assertRaises(FinanceValidationError):
                    normalize_invoice_workflow_payload(payload)

    def test_extracts_base_invoice_number(self) -> None:
        self.assertEqual(
            invoice_base_number("ОМ-13290/1"),
            "ОМ-13290",
        )
        self.assertEqual(
            invoice_base_number("ОМ-13290/12"),
            "ОМ-13290",
        )
        self.assertEqual(
            invoice_base_number("ОМ-13290/исправленный"),
            "ОМ-13290/исправленный",
        )
        self.assertEqual(invoice_base_number(""), "")

    def test_suggests_next_number_from_invoice_not_application(self) -> None:
        result = suggest_next_invoice_number(
            [
                {
                    "id": 141,
                    "invoice_no": "ОМ-13290/1",
                    "base_invoice_no": "ОМ-13290/1",
                },
            ],
        )

        self.assertEqual(result, "ОМ-13290/2")

    def test_suggestion_uses_max_suffix_and_ignores_non_series_values(
        self,
    ) -> None:
        result = suggest_next_invoice_number(
            [
                {"id": 10, "invoice_no": "ОМ-50/2"},
                {"id": 11, "invoice_no": "ОМ-50/7"},
                {"id": 1, "invoice_no": "ОМ-500/20"},
            ],
        )

        self.assertEqual(result, "ОМ-50/8")

    def test_uses_latest_invoice_series_and_handles_empty_list(
        self,
    ) -> None:
        self.assertEqual(
            suggest_next_invoice_number([
                {"id": 1, "invoice_no": "СТАРАЯ/4"},
                {"id": 2, "invoice_no": "НОВАЯ/1"},
            ]),
            "НОВАЯ/2",
        )
        self.assertEqual(suggest_next_invoice_number([]), "")

    def test_unnumbered_existing_invoice_is_first_in_series(self) -> None:
        self.assertEqual(
            suggest_next_invoice_number([
                {"id": 1, "invoice_no": "ОМ-13290"},
            ]),
            "ОМ-13290/2",
        )

    def test_normalizes_complete_invoice(self) -> None:
        result = normalize_invoice_payload({
            "invoice_no": "  OM-100/1  ",
            "content": "  Апрель  ",
            "amount": "62 220,50",
            "service_start_date": "2026-04-01",
            "service_end_date": "2026-04-30",
            "due_date": "",
        })

        self.assertEqual(
            result,
            {
                "invoice_no": "OM-100/1",
                "content": "Апрель",
                "amount": "62220.50",
                "service_start_date": "2026-04-01",
                "service_end_date": "2026-04-30",
                "due_date": None,
            },
        )

    def test_rejects_missing_fields_and_invalid_service_dates(self) -> None:
        invalid = [
            {
                "invoice_no": "",
                "content": "Май",
                "amount": "100",
            },
            {
                "invoice_no": "OM-1",
                "content": "",
                "amount": "100",
            },
            {
                "invoice_no": "OM-1",
                "content": "Май",
                "amount": "0",
            },
            {
                "invoice_no": "OM-1",
                "content": "Май",
                "amount": "100",
                "service_start_date": "2026-06-01",
                "service_end_date": "2026-05-31",
            },
        ]

        for payload in invalid:
            with self.subTest(payload=payload):
                with self.assertRaises(FinanceValidationError):
                    normalize_invoice_payload(payload)


class PaymentValidationTests(unittest.TestCase):
    def test_normalizes_payment_and_allows_empty_date(self) -> None:
        result = normalize_payment_payload({
            "amount": " 10 000,25 ",
            "payment_date": "",
        })

        self.assertEqual(
            result,
            {
                "amount": "10000.25",
                "payment_date": None,
            },
        )

    def test_rejects_non_positive_or_invalid_amount(self) -> None:
        for amount in ("", "текст", "-1", "0", "NaN"):
            with self.subTest(amount=amount):
                with self.assertRaises(FinanceValidationError):
                    normalize_payment_payload({"amount": amount})


class UpdValidationTests(unittest.TestCase):
    def test_normalizes_complete_or_empty_upd(self) -> None:
        self.assertEqual(
            normalize_upd_payload({
                "upd_no": "  13290/2  ",
                "shipment_date": "2026-09-30",
                "sent_at": "2026-10-01",
                "signed_at": "",
            }),
            {
                "upd_no": "13290/2",
                "shipment_date": "2026-09-30",
                "sent_at": "2026-10-01",
                "signed_at": None,
            },
        )
        self.assertEqual(
            normalize_upd_payload({}),
            {
                "upd_no": None,
                "shipment_date": None,
                "sent_at": None,
                "signed_at": None,
            },
        )

    def test_rejects_invalid_upd_dates_and_payload(self) -> None:
        invalid_payloads = (
            {"shipment_date": "30.09.2026"},
            {"sent_at": "1999-12-31"},
            {"signed_at": "2101-01-01"},
            "УПД",
        )

        for payload in invalid_payloads:
            with self.subTest(payload=payload):
                with self.assertRaises(FinanceValidationError):
                    normalize_upd_payload(payload)


if __name__ == "__main__":
    unittest.main()
