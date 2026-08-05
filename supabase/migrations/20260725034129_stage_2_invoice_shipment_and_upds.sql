-- Billing Control v2 — этап 2.
-- Добавляющая миграция даты отгрузки и базовой целостности УПД.
-- Не удаляет и не переименовывает существующие объекты.

do $preflight$
begin
    if to_regclass('public.invoices') is null then
        raise exception 'Не найдена таблица public.invoices';
    end if;

    if to_regclass('public.upds') is null then
        raise exception 'Не найдена таблица public.upds';
    end if;

    if to_regclass('public.photo_reports') is null then
        raise exception 'Не найдена таблица public.photo_reports';
    end if;

    if exists (
        select 1
        from information_schema.columns
        where table_schema = 'public'
          and table_name = 'invoices'
          and column_name = 'shipment_date'
          and (
              data_type <> 'date'
              or is_nullable <> 'YES'
          )
    ) then
        raise exception
            'public.invoices.shipment_date уже существует с несовместимым типом или обязательностью';
    end if;

    if exists (
        select 1
        from public.photo_reports
        where not_required
          and url is not null
    ) then
        raise exception
            'Есть фотоотчёты, где одновременно заполнена ссылка и установлен признак «не требуется»';
    end if;

    if exists (
        select invoice_id
        from public.upds
        group by invoice_id
        having count(*) > 1
    ) then
        raise exception 'Обнаружено несколько УПД для одного счёта';
    end if;

    if exists (
        select 1
        from public.upds u
        left join public.invoices i on i.id = u.invoice_id
        where i.id is null
    ) then
        raise exception 'Обнаружен УПД без существующего счёта';
    end if;
end;
$preflight$;

alter table public.invoices
    add column if not exists shipment_date date null;

comment on column public.invoices.shipment_date is
    'Дата отгрузки счёта; является датой связанного УПД.';

do $add_photo_constraint$
begin
    if not exists (
        select 1
        from pg_constraint
        where conrelid = 'public.photo_reports'::regclass
          and conname = 'photo_reports_not_required_without_url'
    ) then
        alter table public.photo_reports
            add constraint photo_reports_not_required_without_url
            check (not not_required or url is null)
            not valid;
    end if;
end;
$add_photo_constraint$;

alter table public.photo_reports
    validate constraint photo_reports_not_required_without_url;

insert into public.upds (invoice_id, upd_date)
select i.id, i.shipment_date
from public.invoices i
left join public.upds u on u.invoice_id = i.id
where u.id is null
on conflict (invoice_id) do nothing;

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
        select 1
        from public.upds u
        join public.invoices i on i.id = u.invoice_id
        where u.upd_date is distinct from i.shipment_date
    ) then
        raise exception 'Дата существующего УПД не совпадает с датой отгрузки счёта';
    end if;
end;
$postflight$;
