import json, os, datetime

from fastapi import FastAPI, Request, Form, Query

from fastapi.responses import HTMLResponse, RedirectResponse



app = FastAPI()

DB_FILE = "database.json"

MONTHS_ORDER = ["ЯНВ", "ФЕВ", "МАР", "АПР", "МАЙ", "ИЮН", "ИЮЛ", "АВГ", "СЕН", "ОКТ", "НОЯ", "ДЕК"]



def load_db():

    if not os.path.exists(DB_FILE): return []

    with open(DB_FILE, "r", encoding="utf-8") as f: return json.load(f)



def save_db(data):

    with open(DB_FILE, "w", encoding="utf-8") as f: json.dump(data, f, ensure_ascii=False, indent=4)



def format_curr(value):

    try:

        val = float(str(value).replace(' ', '').replace(',', '.'))

        return "{:,.0f}".format(val).replace(",", " ")

    except: return "0"



def get_sort_weight(period_str):

    if not period_str: return (-1, 0)

    months = [m for m in MONTHS_ORDER if m in str(period_str).upper()]

    if not months: return (-1, 0)

    # Первый элемент кортежа — индекс последнего месяца (для хронологии)

    # Второй элемент — инвертированное кол-во месяцев (чем меньше месяцев, тем выше вес)

    last_month_idx = max([MONTHS_ORDER.index(m) for m in months])

    count_weight = 100 - len(months)

    return (last_month_idx, count_weight)



@app.get("/", response_class=HTMLResponse)

async def dashboard(request: Request, mode: str = Query(None)):

    db_data = load_db()

    today = datetime.date.today()

   

    # СОРТИРОВКА: 1. Период (одиночные выше) 2. Номер приложения (APP)

    db_data.sort(key=lambda x: (

        get_sort_weight(x.get('period', '')),

        x.get('app_no', '0').zfill(5)

    ), reverse=True)

   

    is_readonly = (mode == "read")

    all_entities = sorted(list(set(str(item.get('entity', '-')).strip() for item in db_data if item.get('entity'))))

    target_statuses = ["-", "ОТПРАВЛЕНО", "ПОДПИСАНО", "ОПЛАЧЕНО", "ПРОСРОЧКА"]



    rows_html = ""

    for item in db_data:
        idx = item['id']
        service, entity = item.get('service', '-'), item.get('entity', '-')
        bill_no, app_no = item.get('bill_no', '-'), item.get('app_no', '-')
        period = item.get('period', '-')
        total_sum = float(item.get('total_sum', 0))
        payments = item.get('payments', [])
        photo_rows = ""
        for m in MONTHS_ORDER:
           if m in period:
              photo_rows += f"""
    <div class="border rounded-xl p-3 space-y-2">
        <div class="text-[10px] font-black text-slate-700 uppercase">
             {m}
        </div>
        {
        f'''
        <div class="flex gap-3 items-center">

<a
href="{item.get("photo_reports",{}).get(m,{}).get("url","")}"
target="_blank"
class="text-[10px] text-indigo-500 font-black hover:underline"
>
Открыть ссылку
</a>

<a
href="#"
onclick="
this.parentElement.innerHTML=
`<input
name='photo_url_{m}'
value='{item.get("photo_reports",{}).get(m,{}).get("url","")}'
placeholder='https://cloud.mail.ru/...'
class='w-full p-2 border rounded-lg text-[9px]'
form='edit-form-{idx}'
>`;
return false;
"
class="text-[9px] text-slate-500 hover:underline"
>
Изменить / удалить
</a>

</div>
        '''
        if item.get("photo_reports",{}).get(m,{}).get("url")
        and not item.get("photo_reports",{}).get(m,{}).get("disabled")
        else
        f'''
<input
name="photo_url_{m}"
value=""
placeholder="https://cloud.mail.ru/..."
class="w-full p-2 border rounded-lg text-[9px]"
form="edit-form-{idx}"
>
'''
if not item.get("photo_reports",{}).get(m,{}).get("disabled")
else ""
}
        <label class="flex items-center gap-2 text-[9px] text-slate-500">
             <input
                 type="checkbox"
                  name="photo_disabled_{m}"
                  {"checked" if item.get("photo_reports",{}).get(m,{}).get("disabled") else ""}
                  form="edit-form-{idx}"
             >

             ФО не нужен

        </label>
       
    </div>
    """
        paid_sum = 0
        has_overdue = False
        pay_rows = ""

       

        for p_idx, p in enumerate(payments):

            st = p.get('status')

            p_amt = float(str(p.get('amount', 0)).replace(' ', '').replace(',', '.'))

            if st == 'paid': paid_sum += p_amt

           

            p_date_str = p.get('date', '')

            if p_date_str:

                try:

                    p_date = datetime.date.fromisoformat(p_date_str) if '-' in p_date_str else datetime.datetime.strptime(p_date_str, "%d.%m.%Y").date()

                    if st != 'paid' and p_date < today: has_overdue = True

                except: pass



            # ЛОГИКА ОТОБРАЖЕНИЯ СТАТУСА ПЛАТЕЖА

            if is_readonly:

                status_label = '<span class="text-emerald-500 font-bold">ОПЛАЧЕНО</span>' if st == 'paid' else '<span class="text-slate-400">ОЖИДАЕТ</span>'

                pay_action_html = f'<div class="text-[9px] border rounded px-2 py-0.5 bg-white">{status_label}</div>'

            else:

                pay_action_html = f'''

                <form action="/toggle_pay/{idx}/{p_idx}" method="post" class="flex gap-1 items-center">

                    <select name="new_status" class="text-[9px] border rounded {'text-emerald-500 font-bold' if st=='paid' else ''}">

                        <option value="waiting" {'selected' if st!='paid' else ''}>ОЖИДАЕТ</option>

                        <option value="paid" {'selected' if st=='paid' else ''}>ОПЛАЧЕНО</option>

                    </select>

                    <button class="bg-indigo-600 text-white px-2 py-0.5 rounded text-[8px]">OK</button>

                </form>'''



            pay_rows += f"""

            <div class="flex justify-between items-center py-2 border-b border-slate-50 text-[10px]">

                <form action="/edit_pay_detail/{idx}/{p_idx}" method="post" class="flex items-center gap-2 flex-1">

                    <input type="date" name="date" value="{p_date_str}" {"disabled" if is_readonly else ""} class="border rounded px-1 py-0.5 text-[9px] font-bold text-indigo-600 disabled:bg-transparent disabled:border-none">

                    <span class="font-bold uppercase text-slate-700">{p['comment']}</span>

                    <span class="text-slate-400">| {format_curr(p_amt)} ₽</span>

                    {'' if is_readonly else '<button class="ml-1 opacity-50 hover:opacity-100">💾</button>'}

                </form>

                <div class="flex gap-1 items-center">

                    {pay_action_html}

                    {'' if is_readonly else f'<a href="/delete_payment/{idx}/{p_idx}" class="text-red-300 hover:text-red-500 ml-1">✕</a>'}

                </div>

            </div>"""



        debt = total_sum - paid_sum

        display_status = "ОПЛАЧЕНО" if total_sum > 0 and debt <= 0.1 else ("ПРОСРОЧКА" if has_overdue else str(item.get('status', '-')).upper())

       

        # Новая логика цветов:

        if display_status == "ОПЛАЧЕНО":

            status_class = "text-emerald-500"

        elif display_status == "ПРОСРОЧКА":

            status_class = "text-red-500 animate-pulse font-black"

        elif display_status == "ПОДПИСАНО":

            status_class = "text-amber-500" # Тот самый желтый (amber)

        else:

            status_class = "text-slate-500"



        month_checks = "".join([f"""

            <label class="flex items-center gap-1 bg-slate-50 px-2 py-1 rounded border border-slate-100 cursor-pointer">

                <input type="checkbox" name="months" value="{m}" {"checked" if m in period else ""} class="w-3 h-3">

                <span class="text-[9px] font-bold">{m}</span>

            </label>""" for m in MONTHS_ORDER])


        rows_html += f"""

        <tbody class="item-row border-b border-slate-50" id="row-{idx}" data-entity="{entity}" data-period="{period}" data-status="{display_status}">

            <tr class="hover:bg-indigo-50/30 cursor-pointer" onclick="if(!event.target.closest('form') && !event.target.closest('a')) toggleEdit('{idx}')">

                <td class="px-6 py-6 text-center font-bold text-indigo-400 text-xs italic">№ {app_no}</td>

                <td class="px-6 py-6 text-center text-slate-400 text-xs">{bill_no}</td>

                <td class="px-6 py-6"><div class="font-bold text-slate-800 text-sm">{service}</div><div class="text-[10px] text-slate-400 uppercase font-bold">{entity}</div></td>

                <td class="px-6 py-6 text-center"><span class="px-3 py-1 bg-white border rounded-lg text-[9px] font-black text-slate-500 uppercase">{period or '-'}</span></td>

                <td class="px-6 py-6 text-center text-[10px] font-black uppercase {status_class}">{display_status}</td>

                <td class="px-6 py-6 text-right font-mono text-xs text-slate-400">{format_curr(total_sum)}</td>

                <td class="px-6 py-6 text-right font-mono text-sm font-black {'text-emerald-500' if debt<=0.1 else 'text-red-500'}">{format_curr(debt)}</td>

                <td class="px-4 py-6 text-center">{f'<a href="/delete_item/{idx}" class="text-slate-200 hover:text-red-500">✕</a>' if not is_readonly else ""}</td>

            </tr>

            <tr id="edit-{idx}" class="hidden">

                <td colspan="8" class="bg-slate-50/50 p-8 border-t border-slate-100">

                    <div class="grid grid-cols-3 gap-8 max-w-7xl mx-auto">

                        <form action="/update_item/{idx}" method="post" class="space-y-4" id="edit-form-{idx}">

                            <p class="text-[10px] font-black text-indigo-600 uppercase italic">Настройки</p>

                            <input name="service" value="{service}" {"readonly" if is_readonly else ""} class="w-full p-4 bg-white border rounded-2xl text-xs shadow-sm" placeholder="Услуга">

                            <input name="entity" value="{entity}" {"readonly" if is_readonly else ""} class="w-full p-4 bg-white border rounded-2xl text-xs shadow-sm" placeholder="Юридическое лицо (ЮЛ)">

                            <div class="grid grid-cols-2 gap-4">

                                <input name="app_no" value="{app_no}" {"readonly" if is_readonly else ""} class="p-4 bg-white border rounded-2xl text-xs shadow-sm" placeholder="APP">

                                <input name="bill_no" value="{bill_no}" {"readonly" if is_readonly else ""} class="p-4 bg-white border rounded-2xl text-xs shadow-sm" placeholder="INV">

                            </div>

                            <input
                            name="status"
                            value="{item.get('status','')}"
                            placeholder="Статус приложения"
                            class="w-full p-3 border rounded-2xl"
                            form="edit-form-{idx}"
                            {"readonly" if is_readonly else ""}
                            >

                            <div class="grid grid-cols-6 gap-1 bg-white p-3 rounded-2xl border shadow-sm {"pointer-events-none opacity-80" if is_readonly else ""}">{month_checks}</div>

                            <input name="total_sum" type="number" step="0.01" value="{total_sum}" {"readonly" if is_readonly else ""} class="w-full p-4 bg-white border rounded-2xl text-xs font-mono shadow-sm">

                            {'' if is_readonly else '<button class="w-full bg-[#1e1b4b] text-white p-5 rounded-2xl text-[10px] font-black uppercase">Обновить данные</button>'}

                       </form>
                        <div class="bg-white p-6 rounded-2xl border shadow-sm">

                            <p class="text-[10px] font-black text-emerald-500 uppercase italic mb-4">График платежей</p>

                            <div class="space-y-1">{pay_rows}</div>

                            {'' if is_readonly else f'''

                            <form action="/add_payment/{idx}" method="post" class="grid grid-cols-2 gap-2 mt-6 pt-6 border-t">

                                <input name="comment" placeholder="Описание" class="col-span-2 p-3 border rounded-xl text-[10px]" required>

                                <input name="amount" type="number" step="0.01" placeholder="Сумма" class="p-3 border rounded-xl text-[10px]" required>

                                <input name="date" type="date" class="p-3 border rounded-xl text-[10px]" required>

                                <button class="bg-emerald-500 text-white rounded-xl font-black text-[9px] uppercase col-span-2 py-3 shadow-lg">+ Добавить</button>

                            </form>'''}

                        </div>
                        <div class="bg-white p-6 rounded-2xl border shadow-sm">

                         <p class="text-[10px] font-black text-indigo-500 uppercase italic mb-4">
                             Фотоотчеты
                         </p>
<div class="space-y-3">

    {photo_rows if photo_rows else '''

    <div class="border rounded-xl p-3">

        <div class="text-[10px] font-black text-slate-400 uppercase">

            Нет месяцев

        </div>

    </div>

    '''}

</div>

</div>

                    </div>

                </td>

            </tr>

        </tbody>"""



    return f"""

    <!DOCTYPE html><html><head><meta charset="UTF-8"><script src="https://cdn.tailwindcss.com"></script>

    <style>@import url('https://fonts.googleapis.com/css2?family=Plus+Jakarta+Sans:wght@600;800&display=swap');

    body {{ font-family: 'Plus Jakarta Sans', sans-serif; background: #fcfdfe; }} .hidden {{ display: none; }}

    .active-filter {{ background: #4f46e5 !important; color: white !important; transform: translateY(-2px); }}

    </style></head><body class="p-12"><div class="max-w-7xl mx-auto">

        <div class="flex justify-between items-end mb-12">

            <h1 class="text-4xl font-black text-[#1e1b4b] uppercase italic tracking-tighter">Billing <span class="text-indigo-600">Control</span></h1>

            {f'<a href="/add_item" class="bg-[#1e1b4b] text-white px-8 py-4 rounded-2xl text-[11px] font-black uppercase shadow-xl">+ Новая запись</a>' if not is_readonly else ""}

        </div>

        <div class="bg-[#eef4ff] rounded-[40px] p-10 mb-12 border border-indigo-50 grid grid-cols-3 gap-10">

            <div><p class="text-[10px] font-black text-indigo-400 uppercase mb-5 italic">Контрагент</p><div class="flex flex-wrap gap-2">{"".join([f'<button data-type="entity" data-val="{e}" class="filter-btn px-4 py-2.5 bg-white rounded-xl text-[10px] font-bold text-slate-500 border border-slate-100 uppercase shadow-sm">{e}</button>' for e in all_entities])}</div></div>

            <div><p class="text-[10px] font-black text-indigo-400 uppercase mb-5 italic">Месяц</p><div class="grid grid-cols-6 gap-2">{"".join([f'<button data-type="period" data-val="{m}" class="filter-btn px-3 py-2.5 bg-white rounded-xl text-[9px] font-bold text-slate-400 border border-slate-100 uppercase shadow-sm">{m}</button>' for m in MONTHS_ORDER])}</div></div>

            <div><p class="text-[10px] font-black text-indigo-400 uppercase mb-5 italic">Статус</p><div class="flex flex-wrap gap-2">{"".join([f'<button data-type="status" data-val="{s}" class="filter-btn px-4 py-2.5 bg-white rounded-xl text-[10px] font-bold text-slate-500 border border-slate-100 uppercase shadow-sm">{s}</button>' for s in target_statuses])}</div></div>

        </div>

        <div class="bg-white rounded-[40px] shadow-sm border border-slate-100 overflow-hidden">

            <table class="w-full text-left">

                <thead class="bg-slate-50/50 border-b border-slate-100 text-[10px] font-black text-slate-400 uppercase italic">

                    <tr><th class="px-6 py-7 text-center">APP</th><th class="px-6 py-7 text-center">INV</th><th class="px-6 py-7">Услуга</th><th class="px-6 py-7 text-center">Период</th><th class="px-6 py-7 text-center">Статус</th><th class="px-6 py-7 text-right text-slate-400">Сумма</th><th class="px-6 py-7 text-right text-slate-900">Долг</th><th class="px-4 py-7"></th></tr>

                </thead>

                {rows_html}

            </table>

        </div>

    </div>

    <script>

        let filters = {{entity: '', period: '', status: ''}};

        function toggleEdit(id) {{ document.getElementById('edit-'+id).classList.toggle('hidden'); }}

        function applyFilters() {{

            document.querySelectorAll('.filter-btn').forEach(btn => btn.classList.toggle('active-filter', filters[btn.dataset.type] === btn.dataset.val));

            document.querySelectorAll('.item-row').forEach(row => {{

                const mE = !filters.entity || row.dataset.entity === filters.entity;

                const mS = !filters.status || row.dataset.status === filters.status;

                const mP = !filters.period || row.dataset.period.includes(filters.period.toUpperCase());

                row.style.display = (mE && mS && mP) ? "" : "none";

            }});

            localStorage.setItem('billing_filters', JSON.stringify(filters));

        }}

        document.querySelectorAll('.filter-btn').forEach(btn => {{

            btn.onclick = () => {{

                const t = btn.dataset.type;

                filters[t] = (filters[t] === btn.dataset.val) ? '' : btn.dataset.val;

                applyFilters();

            }};

        }});

        window.onload = () => {{

            const saved = localStorage.getItem('billing_filters');

            if (saved) filters = JSON.parse(saved);

            applyFilters();

            const hash = window.location.hash;

            if (hash) {{

                const id = hash.replace('#row-', '');

                const editRow = document.getElementById('edit-' + id);

                if (editRow) {{

                    editRow.classList.remove('hidden');

                    document.getElementById('row-' + id).scrollIntoView({{behavior: 'smooth', block: 'center'}});

                }}

            }}

        }};

    </script></body></html>"""



@app.post("/edit_pay_detail/{item_id}/{p_idx}")

async def edit_pay_detail(item_id: int, p_idx: int, date: str = Form(...)):

    db = load_db()

    for i in db:

        if i['id'] == item_id: i['payments'][p_idx]['date'] = date

    save_db(db)

    return RedirectResponse(url=f"/?#row-{item_id}", status_code=303)



@app.post("/update_item/{item_id}")
async def update_item(item_id: int, request: Request):
    form = await request.form()
    db = load_db()
    for i in db:
        if i['id'] == item_id:
            i['service'], i['app_no'], i['bill_no'] = form.get('service'), form.get('app_no'), form.get('bill_no')
            i['total_sum'] = float(str(form.get('total_sum', 0)).replace(',', '.'))
            i['period'] = ", ".join(form.getlist('months'))
            photo_reports = {}
            for m in MONTHS_ORDER:
             if m in form.getlist('months'):
                 photo_reports[m] = {
                     "url": form.get(f'photo_url_{m}',''),
                      "disabled": f'photo_disabled_{m}' in form
                 }
            i['photo_reports'] = photo_reports
    save_db(db)
    return RedirectResponse(url=f"/?#row-{item_id}", status_code=303)

@app.post("/add_payment/{item_id}")
async def add_payment(item_id: int, amount: float=Form(...), comment: str=Form(...), date: str=Form(...)):
    db = load_db()
    for i in db:
        if i['id'] == item_id:
            if 'payments' not in i: i['payments'] = []
            i['payments'].append({"amount": amount, "comment": comment, "date": date, "status": "waiting"})
    save_db(db)
    return RedirectResponse(url=f"/?#row-{item_id}", status_code=303)


@app.post("/toggle_pay/{item_id}/{p_idx}")
async def toggle_pay(item_id: int, p_idx: int, new_status: str=Form(...)):
    db = load_db()
    for i in db:
        if i['id'] == item_id: i['payments'][p_idx]['status'] = new_status
    save_db(db)
    return RedirectResponse(url=f"/?#row-{item_id}", status_code=303)

@app.get("/delete_payment/{item_id}/{p_idx}")
async def delete_payment(item_id: int, p_idx: int):
    db = load_db()
    for i in db:
        if i['id'] == item_id: i['payments'].pop(p_idx)
    save_db(db)
    return RedirectResponse(url=f"/?#row-{item_id}")

@app.get("/add_item")
async def add_item():
    db = load_db()
    new_id = max([i['id'] for i in db] + [0]) + 1
    db.append({"id": new_id, "app_no": "Приложение", "bill_no": "Счет", "service": "Новая запись", "entity": "ЮЛ", "period": "", "status": "-", "total_sum": 0, "payments": []})
    save_db(db)
    return RedirectResponse(url="/")

@app.get("/delete_item/{item_id}")
async def delete_item(item_id: int):
    db = load_db()
    save_db([i for i in db if i['id'] != item_id])
    return RedirectResponse(url="/")


if __name__ == "__main__":

    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8001)