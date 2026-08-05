-- Billing Control v2 — этап 5.
-- Автоматическое создание УПД и атомарное сохранение его ручных полей.
-- Не удаляет и не переименовывает существующие данные.

begin;

do $preflight$
begin
    if to_regclass('public.invoices') is null then
        raise exception 'Не найдена таблица public.invoices';
    end if;

    if to_regclass('public.upds') is null then
        raise exception 'Не найдена таблица public.upds';
    end if;

    if not exists (
        select 1
        from information_schema.columns
        where table_schema = 'public'
          and table_name = 'invoices'
          and column_name = 'shipment_date'
          and data_type = 'date'
          and is_nullable = 'YES'
    ) then
        raise exception
            'Не найдена nullable-колонка public.invoices.shipment_date типа date';
    end if;

    if not exists (
        select 1
        from pg_constraint
        where conrelid = 'public.upds'::regclass
          and contype = 'u'
          and conkey = array[
              (
                  select attnum
                  from pg_attribute
                  where attrelid = 'public.upds'::regclass
                    and attname = 'invoice_id'
              )
          ]::smallint[]
    ) then
        raise exception
            'На public.upds.invoice_id отсутствует уникальное ограничение';
    end if;
end;
$preflight$;

create or replace function public.sync_invoice_upd()
returns trigger
language plpgsql
security invoker
set search_path = ''
as $function$
begin
    insert into public.upds (
        invoice_id,
        upd_date
    )
    values (
        new.id,
        new.shipment_date
    )
    on conflict (invoice_id) do update
    set upd_date = excluded.upd_date;

    return new;
end;
$function$;

revoke all on function public.sync_invoice_upd() from public;
revoke all on function public.sync_invoice_upd() from anon;
revoke all on function public.sync_invoice_upd() from authenticated;
grant execute on function public.sync_invoice_upd() to service_role;

drop trigger if exists sync_invoice_upd_after_write
on public.invoices;

create trigger sync_invoice_upd_after_write
after insert or update of shipment_date
on public.invoices
for each row
execute function public.sync_invoice_upd();

insert into public.upds (
    invoice_id,
    upd_date
)
select
    i.id,
    i.shipment_date
from public.invoices i
left join public.upds u on u.invoice_id = i.id
where u.id is null
on conflict (invoice_id) do nothing;

create or replace function public.save_invoice_upd(
    p_application_id bigint,
    p_invoice_id bigint,
    p_upd_no text,
    p_shipment_date date,
    p_sent_at date,
    p_signed_at date
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
    select i.id
    into v_invoice_id
    from public.invoices i
    where i.id = p_invoice_id
      and i.application_id = p_application_id
    for update;

    if v_invoice_id is null then
        raise exception using
            errcode = 'P0002',
            message = 'Счёт не найден в указанном приложении';
    end if;

    update public.invoices
    set shipment_date = p_shipment_date
    where id = p_invoice_id;

    insert into public.upds (
        invoice_id,
        upd_no,
        upd_date,
        sent_at,
        signed_at
    )
    values (
        p_invoice_id,
        nullif(btrim(p_upd_no), ''),
        p_shipment_date,
        p_sent_at,
        p_signed_at
    )
    on conflict (invoice_id) do update
    set
        upd_no = excluded.upd_no,
        upd_date = excluded.upd_date,
        sent_at = excluded.sent_at,
        signed_at = excluded.signed_at
    returning id into v_upd_id;

    return jsonb_build_object(
        'status', 'saved',
        'invoice_id', p_invoice_id,
        'upd_id', v_upd_id
    );
end;
$function$;

revoke all on function public.save_invoice_upd(
    bigint,
    bigint,
    text,
    date,
    date,
    date
) from public;
revoke all on function public.save_invoice_upd(
    bigint,
    bigint,
    text,
    date,
    date,
    date
) from anon;
revoke all on function public.save_invoice_upd(
    bigint,
    bigint,
    text,
    date,
    date,
    date
) from authenticated;
grant execute on function public.save_invoice_upd(
    bigint,
    bigint,
    text,
    date,
    date,
    date
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
        raise exception 'После миграции есть несколько УПД у одного счёта';
    end if;

    if exists (
        select 1
        from public.upds u
        join public.invoices i on i.id = u.invoice_id
        where u.upd_date is distinct from i.shipment_date
    ) then
        raise exception
            'После миграции дата УПД расходится с датой отгрузки счёта';
    end if;
end;
$postflight$;

commit;
