-- Атомарное создание счёта и его рабочего статуса УПД.

begin;

create or replace function public.create_invoice_workflow(
    p_application_id bigint,
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
    v_legal_entity_id bigint;
    v_content text;
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

    select
        a.legal_entity_id,
        a.content
    into
        v_legal_entity_id,
        v_content
    from public.applications a
    where a.id = p_application_id
    for share;

    if v_legal_entity_id is null then
        raise exception using
            errcode = 'P0002',
            message = 'Приложение не найдено';
    end if;

    insert into public.invoices (
        legal_entity_id,
        application_id,
        invoice_no,
        base_invoice_no,
        content,
        amount,
        service_start_date,
        service_end_date,
        due_date,
        shipment_date,
        is_paid
    )
    values (
        v_legal_entity_id,
        p_application_id,
        btrim(p_invoice_no),
        btrim(p_base_invoice_no),
        v_content,
        p_amount,
        null,
        null,
        p_due_date,
        p_shipment_date,
        p_is_paid
    )
    returning id into v_invoice_id;

    insert into public.upds (
        invoice_id,
        upd_date,
        status
    )
    values (
        v_invoice_id,
        p_shipment_date,
        p_upd_status
    )
    on conflict (invoice_id) do update
    set
        upd_date = excluded.upd_date,
        status = excluded.status
    returning id into v_upd_id;

    return jsonb_build_object(
        'status', 'created',
        'invoice_id', v_invoice_id,
        'upd_id', v_upd_id
    );
end;
$function$;

revoke all on function public.create_invoice_workflow(
    bigint,
    text,
    text,
    numeric,
    date,
    date,
    boolean,
    text
) from public;
revoke all on function public.create_invoice_workflow(
    bigint,
    text,
    text,
    numeric,
    date,
    date,
    boolean,
    text
) from anon;
revoke all on function public.create_invoice_workflow(
    bigint,
    text,
    text,
    numeric,
    date,
    date,
    boolean,
    text
) from authenticated;
grant execute on function public.create_invoice_workflow(
    bigint,
    text,
    text,
    numeric,
    date,
    date,
    boolean,
    text
) to service_role;

commit;
