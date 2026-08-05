# Billing Control v2 — состав результатов

Актуальное состояние: карточка приложения полностью перестроена под рабочий
процесс. Приложение остаётся верхним объектом, периоды и фотоотчёты —
отдельными блоками, а номер, сумма, срок оплаты, отметка оплаты, дата
отгрузки и статус УПД объединены в одной строке счёта.

## 01 Актуальная версия

- `app.py` — основной интерфейс приложения.
- `app_mode.py` — безопасное переключение режимов `read` / `edit`.
- `finance_records.py` — проверка данных счетов и платежей.
- `periods_photos.py` — проверка периодов и фотоотчётов.
- `audit_database.py` — аудит данных Supabase и сверка с исходным JSON.
- `requirements.txt` — зависимости Python.
- `.env.example` — пример настроек без ключей и секретов.

## 02 Миграции

- `20260725034129_stage_2_invoice_shipment_and_upds.sql` — изменения этапа 2.
- `stage_4_periods_photo_reports.sql` — атомарное сохранение периодов,
  синхронизация фотоотчётов и ограничение URL.
- `stage_5_invoice_upds.sql` — УПД для всех существующих и будущих счетов,
  автоматическое создание и атомарное сохранение.
- `stage_6_application_card_redesign.sql` — признак полной оплаты, четыре
  статуса УПД и атомарное сохранение приложения и строки счёта.
- `stage_6_atomic_invoice_creation.sql` — создание нового счёта и его
  статуса УПД в одной транзакции.
- `stage_6_restrict_rls_event_function.sql` — закрытие внешнего вызова
  внутренней event-trigger функции RLS.

## 03 Тесты

- `test_app_mode.py`
- `test_application_dates.py`
- `test_interface_state.py`
- `test_compact_layout.py`
- `test_finance_records.py`
- `test_periods_photos.py`
- `test_upds.py`

## 04 Отчёты

- `database_audit_report.md` — исходный отчёт аудита.
- `database_integrity_report.md` — проверка целостности Supabase.
- `legacy_import_comparison_report.md` — сверка Supabase с исходным JSON.

## 05 Документация

- `BILLING_CONTROL_WORK_BRIEF.md` — рабочий бриф проекта.
- `CHANGELOG.md` — журнал изменений.
- `STAGE_2_RESULT.md`
- `STAGE_3_RESULT.md`
- `STAGE_4_RESULT.md`
- `STAGE_4_INTERFACE_FIX.md`
- `STAGE_4_COMPACT_LAYOUT.md`
- `STAGE_4_GROUPED_LAYOUT.md`
- `STAGE_4_FINANCE_PHOTOS.md`
- `STAGE_4_FINANCE_FORM_FIX.md`
- `STAGE_4_FINANCE_DISPLAY_FIX.md`
- `STAGE_4_PAYMENT_TERMS_LAYOUT_FIX.md`
- `STAGE_4_PHOTO_DEADLINE_FIX.md`
- `STAGE_4_FINANCE_COLUMN_SPACING_FIX.md`
- `STAGE_4_APPLICATION_DATES_FIX.md`
- `STAGE_4_APPLICATION_SENT_DATE_FIX.md`
- `STAGE_4_INVOICE_NUMBER_DATE_INPUT_FIX.md`
- `STAGE_4_INVOICE_DUE_DATE_LAYOUT_FIX.md`
- `STAGE_4_INVOICE_EQUAL_COLUMNS_FIX.md`
- `STAGE_4_COMPACT_PHOTOS_COLUMN_FIX.md`
- `STAGE_5_UPDS.md`
- `STAGE_6_CARD_REDESIGN.md`

## 06 Исходные данные

- `database.json`
- `database(1).json`
- `import_data.py`

Оба JSON сохранены: это разные файлы, и ни один не удалён без отдельного
решения.

## 07 Архивы этапов

- `Billing_Control_v2_stage_1.zip`
- `Billing_Control_v2_stage_2.zip`
- `Billing_Control_v2_stage_3.zip`
- `Billing_Control_v2_stage_4.zip`
- `Billing_Control_v2_stage_4_interface_fix.zip`
- `Billing_Control_v2_stage_4_compact_layout.zip`
- `Billing_Control_v2_stage_4_grouped_layout.zip`
- `Billing_Control_v2_stage_4_finance_photos.zip`
- `Billing_Control_v2_stage_4_finance_form_fix.zip`
- `Billing_Control_v2_stage_4_finance_display_fix.zip`
- `Billing_Control_v2_stage_4_payment_terms_layout_fix.zip`
- `Billing_Control_v2_stage_4_photo_deadline_fix.zip`
- `Billing_Control_v2_stage_4_finance_column_spacing_fix.zip`
- `Billing_Control_v2_stage_4_application_dates_fix.zip`
- `Billing_Control_v2_stage_4_application_sent_date_fix.zip`
- `Billing_Control_v2_stage_4_invoice_number_date_input_fix.zip`
- `Billing_Control_v2_stage_4_invoice_due_date_layout_fix.zip`
- `Billing_Control_v2_stage_4_invoice_equal_columns_fix.zip`
- `Billing_Control_v2_stage_4_compact_photos_column_fix.zip`
- `Billing_Control_v2_stage_5_upds.zip`
- `Billing_Control_v2_stage_6_card_redesign.zip`

Для текущей установки используется
`Billing_Control_v2_stage_6_card_redesign.zip`.

## Что намеренно не сохраняется

- `.env` с рабочими настройками;
- ключи Supabase и другие секреты;
- `__pycache__`, временные файлы и локальное виртуальное окружение.
