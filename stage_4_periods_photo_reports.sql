-- Billing Control v2 — этап 4.
-- Атомарное сохранение периодов и синхронизация фотоотчётов.

do $migration$
begin
    if exists (
        select 1
        from public.photo_reports
        where url is not null
          and url !~* '^https?://[^[:space:]]+$'
    ) then
        raise exception
            'Нельзя добавить ограничение URL: найдены некорректные ссылки.';
    end if;

    if not exists (
        select 1
        from pg_constraint
        where conname = 'photo_reports_url_http'
          and conrelid = 'public.photo_reports'::regclass
    ) then
        alter table public.photo_reports
            add constraint photo_reports_url_http
            check (
                url is null
                or url ~* '^https?://[^[:space:]]+$'
            );
    end if;
end
$migration$;

create or replace function public.save_application_periods(
    p_application_id bigint,
    p_periods jsonb,
    p_confirm_remove_filled boolean default false
)
returns jsonb
language plpgsql
security invoker
set search_path = ''
as $function$
declare
    v_desired_months date[];
    v_filled_reports jsonb;
    v_added_reports integer := 0;
    v_removed_reports integer := 0;
    v_period_count integer := 0;
    v_report_count integer := 0;
begin
    if p_application_id is null then
        raise exception 'Не указан идентификатор приложения.';
    end if;

    if not exists (
        select 1
        from public.applications
        where id = p_application_id
    ) then
        raise exception 'Приложение % не найдено.', p_application_id;
    end if;

    if p_periods is null
       or jsonb_typeof(p_periods) <> 'array'
       or jsonb_array_length(p_periods) = 0 then
        raise exception
            'У приложения должен остаться хотя бы один период.';
    end if;

    if jsonb_array_length(p_periods) > 50 then
        raise exception
            'Нельзя сохранить больше 50 периодов за один раз.';
    end if;

    if exists (
        select 1
        from jsonb_array_elements(p_periods) as item(value)
        where jsonb_typeof(item.value) <> 'object'
           or not (item.value ? 'start_date')
           or not (item.value ? 'end_date')
    ) then
        raise exception 'Периоды переданы в неверном формате.';
    end if;

    if exists (
        select 1
        from jsonb_to_recordset(p_periods)
            as item(start_date date, end_date date)
        where item.start_date is null
           or item.end_date is null
           or item.end_date < item.start_date
           or item.start_date < date '2000-01-01'
           or item.end_date > date '2100-12-31'
    ) then
        raise exception 'В списке есть некорректный период.';
    end if;

    if exists (
        select 1
        from jsonb_to_recordset(p_periods)
            as item(start_date date, end_date date)
        group by item.start_date, item.end_date
        having count(*) > 1
    ) then
        raise exception 'В списке есть одинаковые периоды.';
    end if;

    select coalesce(
        array_agg(distinct desired.month_start order by desired.month_start),
        array[]::date[]
    )
    into v_desired_months
    from (
        select month_value::date as month_start
        from jsonb_to_recordset(p_periods)
            as item(start_date date, end_date date)
        cross join lateral generate_series(
            date_trunc('month', item.start_date)::date,
            date_trunc('month', item.end_date)::date,
            interval '1 month'
        ) as month_value
    ) as desired;

    select coalesce(
        jsonb_agg(
            jsonb_build_object(
                'id', report.id,
                'year', report.report_year,
                'month', report.report_month
            )
            order by report.report_year, report.report_month
        ),
        '[]'::jsonb
    )
    into v_filled_reports
    from public.photo_reports as report
    where report.application_id = p_application_id
      and not (
          make_date(report.report_year, report.report_month, 1)
          = any(v_desired_months)
      )
      and (
          nullif(btrim(coalesce(report.url, '')), '') is not null
          or report.not_required
          or nullif(btrim(coalesce(report.comment, '')), '') is not null
      );

    if jsonb_array_length(v_filled_reports) > 0
       and not p_confirm_remove_filled then
        return jsonb_build_object(
            'status', 'confirmation_required',
            'filled_reports', v_filled_reports
        );
    end if;

    delete from public.application_periods as existing
    where existing.application_id = p_application_id
      and not exists (
          select 1
          from jsonb_to_recordset(p_periods)
              as desired(start_date date, end_date date)
          where desired.start_date = existing.start_date
            and desired.end_date = existing.end_date
      );

    insert into public.application_periods (
        application_id,
        start_date,
        end_date
    )
    select
        p_application_id,
        desired.start_date,
        desired.end_date
    from jsonb_to_recordset(p_periods)
        as desired(start_date date, end_date date)
    on conflict (application_id, start_date, end_date) do nothing;

    delete from public.photo_reports as report
    where report.application_id = p_application_id
      and not (
          make_date(report.report_year, report.report_month, 1)
          = any(v_desired_months)
      );

    get diagnostics v_removed_reports = row_count;

    insert into public.photo_reports (
        application_id,
        report_year,
        report_month,
        url,
        not_required,
        comment
    )
    select
        p_application_id,
        extract(year from desired.month_start)::integer,
        extract(month from desired.month_start)::integer,
        null,
        false,
        null
    from unnest(v_desired_months) as desired(month_start)
    on conflict (
        application_id,
        report_year,
        report_month
    ) do nothing;

    get diagnostics v_added_reports = row_count;

    select count(*)
    into v_period_count
    from public.application_periods
    where application_id = p_application_id;

    select count(*)
    into v_report_count
    from public.photo_reports
    where application_id = p_application_id;

    return jsonb_build_object(
        'status', 'saved',
        'application_id', p_application_id,
        'period_count', v_period_count,
        'report_count', v_report_count,
        'added_reports', v_added_reports,
        'removed_reports', v_removed_reports
    );
end
$function$;

revoke all on function public.save_application_periods(
    bigint,
    jsonb,
    boolean
) from public;

revoke all on function public.save_application_periods(
    bigint,
    jsonb,
    boolean
) from anon, authenticated;

grant execute on function public.save_application_periods(
    bigint,
    jsonb,
    boolean
) to service_role;

comment on function public.save_application_periods(
    bigint,
    jsonb,
    boolean
) is
    'Атомарно сохраняет периоды приложения и синхронизирует месяцы фотоотчётов.';
