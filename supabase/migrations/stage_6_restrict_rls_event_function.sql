-- Техническая event-trigger функция вызывается только самой базой.
-- Закрываем прямой RPC-вызов для внешних API-ролей.

begin;

revoke all on function public.rls_auto_enable() from public;
revoke all on function public.rls_auto_enable() from anon;
revoke all on function public.rls_auto_enable() from authenticated;
revoke all on function public.rls_auto_enable() from service_role;
grant execute on function public.rls_auto_enable() to postgres;

commit;
