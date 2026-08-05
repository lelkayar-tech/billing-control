-- Billing Control v2 — полная переработка карточки приложения.
-- Платежи и старые реквизиты УПД сохраняются как архивные данные.
-- Пользователь работает с признаком оплаты счёта и статусом УПД.

begin;

do $preflight$
begin
    if to_regclass('public.applications') is null then
        raise exception 'Не найдена таблица public.applications';
    end if;

    if to_regclass('public.invoices') is null then
        raise exception 'Не найдена таблица public.invoices';
    end if;

    if to_regclass('public.payments') is null then
        raise exception 'Не найдена таблица public.payments';
    end if;

    if to_regclass('public.upds') is null then
        raise exception 'Не найдена таблица public.upds';
    end if;
end;
$preflight$;

alter table public.invoices
    add column if not exists is_paid boolean not null default false;

comment on column public.invoices.is_paid is
    'Ручная отметка полной оплаты счёта в упрощённом интерфейсе.';

alter table public.upds
    add column if not exists status text not null default 'waiting';

do $constraint$
begin
    if not exists (
        select 1
        from pg_constraint
        where conrelid = 'public.upds'::regclass
          and conname = 'upds_status_allowed'
    ) then
        alter table public.upds
            add constraint upds_status_allowed
            check (
                status in ('waiting', 'formed', 'sent', 'signed')
            );
    end if;
end;
$constraint$;

comment on column public.upds.status is
    'Рабочий статус УПД: waiting, formed, sent или signed.';

-- Переносим только однозначные полные оплаты. Историю платежей не меняем.
update public.invoices i
set is_paid = (
    select coalesce(sum(p.amount), 0) >= i.amount
    from public.payments p
    where p.invoice_id = i.id
);

-- Восстанавливаем покрытие один счёт — одна внутренняя запись УПД.
insert into public.upds (
    invoice_id,
    upd_date,
    status
)
select
    i.id,
    i.shipment_date,
    'waiting'
from public.invoices i
left join public.upds u on u.invoice_id = i.id
where u.id is null
on conflict (invoice_id) do nothing;

-- Если старые реквизиты когда-либо были заполнены, сохраняем их смысл.
update public.upds
set status = case
    when signed_at is not null then 'signed'
    when sent_at is not null then 'sent'
    when upd_no is not null then 'formed'
    else 'waiting'
end;

create or replace function public.save_application_card(
    p_application_id bigint,
    p_app_no text,
    p_legal_entity_id bigint,
    p_content text,
    p_total_sum numeric,
    p_sent_at date,
    p_signed_at date
)
returns jsonb
language plpgsql
security invoker
set search_path = ''
as $function$
declare
    v_application_id bigint;
begin
    if nullif(btrim(p_content), '') is null then
        raise exception using
            errcode = '22023',
            message = 'Содержание приложения не заполнено';
    end if;

    if p_total_sum is null or p_total_sum < 0 then
        raise exception using
            errcode = '22023',
            message = 'Сумма приложения не может быть отрицательной';
    end if;

    update public.applications
    set
        app_no = nullif(btrim(p_app_no), ''),
        legal_entity_id = p_legal_entity_id,
        content = btrim(p_content),
        total_sum = p_total_sum,
        sent_at = p_sent_at,
        signed_at = p_signed_at
    where id = p_application_id
    returning id into v_application_id;

    if v_application_id is null then
        raise exception using
            errcode = 'P0002',
            message = 'Приложение не найдено';
    end if;

    -- Скрытые служебные поля счетов остаются согласованы с приложением.
    update public.invoices
    set
        legal_entity_id = p_legal_entity_id,
        content = btrim(p_content)
    where application_id = p_application_id;

    return jsonb_build_object(
        'status', 'saved',
        'application_id', v_application_id
    );
end;
$function$;

revoke all on function public.save_application_card(
    bigint,
    text,
    bigint,
    text,
    numeric,
    date,
    date
) from public;
revoke all on function public.save_application_card(
    bigint,
    text,
    bigint,
    text,
    numeric,
    date,
    date
) from anon;
revoke all on function public.save_application_card(
    bigint,
    text,
    bigint,
    text,
    numeric,
    date,
    date
) from authenticated;
grant execute on function public.save_application_card(
    bigint,
    text,
    bigint,
    text,
    numeric,
    date,
    date
) to service_role;

create or replace function public.save_invoice_workflow(
    p_application_id bigint,
    p_invoice_id bigint,
    p_invoice_no text,
    p_base_invoice_no text,
    p_amount numeric,
    p_due_date date,
    p_shipment_date date,
    p_is_paid boolean,
    p_upd_status text
)
returns jsonb
language plpgsql
security invoker
set search_path = ''
as $function$
declare
    v_invoice_id bigint;
    v_upd_id bigint;
begin
    if nullif(btrim(p_invoice_no), '') is null then
        raise exception using
            errcode = '22023',
            message = 'Номер счёта не заполнен';
    end if;

    if nullif(btrim(p_base_invoice_no), '') is null then
        raise exception using
            errcode = '22023',
            message = 'Базовый номер счёта не заполнен';
    end if;

    if p_amount is null or p_amount <= 0 then
        raise exception using
            errcode = '22023',
            message = 'Сумма счёта должна быть больше нуля';
    end if;

    if p_upd_status not in ('waiting', 'formed', 'sent', 'signed') then
        raise exception using
            errcode = '22023',
            message = 'Недопустимый статус УПД';
    end if;

    update public.invoices
    set
        invoice_no = btrim(p_invoice_no),
        base_invoice_no = btrim(p_base_invoice_no),
        amount = p_amount,
        due_date = p_due_date,
        shipment_date = p_shipment_date,
        is_paid = p_is_paid
    where id = p_invoice_id
      and application_id = p_application_id
    returning id into v_invoice_id;

    if v_invoice_id is null then
        raise exception using
            errcode = 'P0002',
            message = 'Счёт не найден в указанном приложении';
    end if;

    insert into public.upds (
        invoice_id,
        upd_date,
        status
    )
    values (
        p_invoice_id,
        p_shipment_date,
        p_upd_status
    )
    on conflict (invoice_id) do update
    set
        upd_date = excluded.upd_date,
        status = excluded.status
    returning id into v_upd_id;

    return jsonb_build_object(
        'status', 'saved',
        'invoice_id', v_invoice_id,
        'upd_id', v_upd_id
    );
end;
$function$;

revoke all on function public.save_invoice_workflow(
    bigint,
    bigint,
    text,
    text,
    numeric,
    date,
    date,
    boolean,
    text
) from public;
revoke all on function public.save_invoice_workflow(
    bigint,
    bigint,
    text,
    text,
    numeric,
    date,
    date,
    boolean,
    text
) from anon;
revoke all on function public.save_invoice_workflow(
    bigint,
    bigint,
    text,
    text,
    numeric,
    date,
    date,
    boolean,
    text
) from authenticated;
grant execute on function public.save_invoice_workflow(
    bigint,
    bigint,
    text,
    text,
    numeric,
    date,
    date,
    boolean,
    text
) to service_role;

do $postflight$
begin
    if exists (
        select 1
        from public.invoices i
        left join public.upds u on u.invoice_id = i.id
        where u.id is null
    ) then
        raise exception 'После миграции остались счета без УПД';
    end if;

    if exists (
        select invoice_id
        from public.upds
        group by invoice_id
        having count(*) > 1
    ) then
        raise exception 'После миграции есть дубли УПД';
    end if;

    if exists (
        select 1
        from public.invoices
        where is_paid is null
    ) then
        raise exception 'После миграции есть счета без признака оплаты';
    end if;

    if exists (
        select 1
        from public.upds
        where status not in ('waiting', 'formed', 'sent', 'signed')
    ) then
        raise exception 'После миграции есть недопустимые статусы УПД';
    end if;
end;
$postflight$;

commit;
