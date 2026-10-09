#!/usr/bin/env python3
"""Local-only human review UI for curated 66 sales vacancies.

No public interface: bind 127.0.0.1; reach via SSH port forwarding.
Does not modify original RAW; writes SALES_REVIEW and SALES_APPLY.
"""
import datetime as dt
import html
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse
import gspread

IDS = """136674478 137970254 137870575 138241698 137153513 138287926 138178723 137393744 137184043 138200500 137295881 138238223 138239365 137462528 138113941 137306429 137909035 138271285 138271703 137335485 132971370 137092873 138287402 138298980 137522327 137743308 137962247 138307765 138308235 137381434 137386362 137391711 138314892 138315064 138319784 138115291 137539227 138199599 137463421 132060621 135413729 138001301 137631824 138231954 138049280 138060846 138294791 138299263 138300823 137765397 137609723 137397233 137539589 137979539 137964668 138228205 138041046 137518077 138169303 137575552 137644712 136176231 137908607 137204931 138291114 138314539""".split()
assert len(IDS) == len(set(IDS)) == 66

def book():
    creds = os.getenv("GOOGLE_APPLICATION_CREDENTIALS", "/opt/polywork/google-service-account.json")
    return gspread.service_account(filename=creds).open_by_key(os.environ["SPREADSHEET_ID"])

def sheet(b, name, headers):
    try: s=b.worksheet(name)
    except gspread.WorksheetNotFound: s=b.add_worksheet(title=name, rows=300, cols=max(len(headers),10))
    if not s.row_values(1): s.update(range_name="A1", values=[headers])
    return s

def read_data():
    b=book()
    raw=b.worksheet("SALES_RAW_V2").get_all_values()
    byid={r[0]:r for r in raw[1:] if r and r[0]}
    review=sheet(b, "SALES_REVIEW", ["HH ID","Решение","Комментарий","Дата UTC","Вакансия","Ссылка"])
    approvals=sheet(b, "SALES_APPLY", ["HH ID","Вакансия","Компания","Зарплата","Оценка","Ссылка","Статус отклика"])
    decisions={r[0]:{"decision":r[1] if len(r)>1 else "", "comment":r[2] if len(r)>2 else ""} for r in review.get_all_values()[1:] if r and r[0]}
    result=[]
    for i,vid in enumerate(IDS):
        r=byid.get(vid,[])
        def at(n): return r[n] if len(r)>n else ""
        title=at(3)
        product=(title+" "+at(4)).lower()
        pros=[]
        risks=[]
        if any(x in product for x in ("инженер","оборудован","пресейл","проект","тепло","электро","кп","насос")): pros.append("Техническая или проектная тематика — совпадает с целью поиска.")
        if "без холодн" in product or "тепл" in product: pros.append("В названии заявлена работа без холодного поиска или с тёплыми контактами.")
        if any(x in product for x in ("региональный","представитель","активн","привлечен")): risks.append("Возможны поездки или самостоятельный поиск клиентов.")
        if at(6)=="REMOTE_SEARCH": risks.append("Удалёнка пока подтверждена только фильтром RSS, не полным описанием.")
        else: risks.append("Нужно проверить адрес офиса и число выездов в Челябинске.")
        if at(5)=="Не указана": risks.append("Не указана зарплата.")
        result.append({"id":vid,"index":i+1,"title":title or "Карточка пока отсутствует в RAW","company":at(4),
          "salary":at(5),"format":at(6),"score":at(7),"url":at(10) or "https://hh.ru/vacancy/"+vid,
          "rss":at(13),"pros":pros,"risks":risks,"decision":decisions.get(vid,{}),
          "questions":["Сколько составляет фиксированный оклад на руки?","Как рассчитываются KPI и процент, какой реальный доход у сотрудников?",
                       "Откуда приходят клиенты: входящие, действующая база или самостоятельный поиск?",
                       "Сколько исходящих звонков в день и сколько командировок?",
                       "Можно ли работать полностью удалённо из Челябинска?"]})
    return result

def save_decision(vid,decision,comment):
    if vid not in IDS or decision not in ("APPROVED","REJECTED","LATER"): raise ValueError("invalid decision")
    b=book()
    raw=b.worksheet("SALES_RAW_V2").get_all_values()
    source=next((r for r in raw[1:] if r and r[0]==vid),None)
    if not source: raise ValueError("HH ID missing in RAW")
    def at(i):return source[i] if len(source)>i else ""
    review=sheet(b,"SALES_REVIEW",["HH ID","Решение","Комментарий","Дата UTC","Вакансия","Ссылка"])
    records=review.get_all_values()
    record=[vid,decision,comment,dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),at(3),at(10)]
    idx=next((i+1 for i,r in enumerate(records) if i and r and r[0]==vid),None)
    if idx: review.update(range_name=f"A{idx}:F{idx}",values=[record])
    else: review.append_row(record,value_input_option="RAW")
    if decision=="APPROVED":
        apply=sheet(b,"SALES_APPLY",["HH ID","Вакансия","Компания","Зарплата","Оценка","Ссылка","Статус отклика"])
        if vid not in set(apply.col_values(1)):
            apply.append_row([vid,at(3),at(4),at(5),at(7),at(10),"Не откликался"],value_input_option="RAW")
    # If a formerly approved card is rejected, remove it from the active APPLY tab
    # only when its status remains "Не откликался"; never delete actual application history.
    if decision=="REJECTED":
        apply=sheet(b,"SALES_APPLY",["HH ID","Вакансия","Компания","Зарплата","Оценка","Ссылка","Статус отклика"])
        rows=apply.get_all_values()
        for i in range(len(rows)-1,0,-1):
            row=rows[i]
            if row and row[0]==vid and len(row)>6 and row[6]=="Не откликался":
                apply.delete_rows(i+1)

PAGE=r"""<!DOCTYPE html><html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Разбор вакансий</title><style>
:root{font-family:system-ui,-apple-system,"Segoe UI",sans-serif;color:#17212e;background:#f2f5f9}
body{max-width:910px;margin:24px auto;padding:0 18px}article{background:white;border:1px solid #dde2e9;padding:28px;border-radius:16px;box-shadow:0 5px 22px #1220390a}
h1{font-size:24px;margin:0 0 12px}h2{font-size:21px;margin:8px 0}.muted{color:#657184}label{display:block;margin-top:18px;font-weight:650}
textarea{box-sizing:border-box;width:100%;min-height:76px;padding:12px;border:1px solid #cdd5df;border-radius:8px;font:inherit}
a{color:#2563eb}button{font:inherit;border:1px solid #c8d0db;border-radius:9px;padding:11px 14px;background:white;cursor:pointer}
button:hover{background:#f4f6fa}.approve{background:#e0f3e6;border-color:#9ccaaa}.reject{background:#fde5e5;border-color:#e6b4b4}
.toolbar{display:flex;gap:9px;justify-content:space-between;flex-wrap:wrap;margin:15px 0}.actions{display:flex;gap:10px;flex-wrap:wrap;margin-top:18px}
pre{white-space:pre-wrap;font:inherit;background:#f5f6f8;border-radius:10px;padding:14px}.tag{border-radius:20px;background:#edf3ff;padding:4px 10px;font-size:13px}
ul{padding-left:21px;line-height:1.65}.row{display:flex;align-items:center;gap:10px;flex-wrap:wrap}#msg{font-weight:600;margin-top:12px}
</style></head><body><div class="toolbar"><div><h1>Разбор вакансий</h1><div class="muted" id="progress">Загрузка…</div></div><div class="row"><button onclick="step(-1)">← Назад</button><button onclick="step(1)">Вперёд →</button></div></div>
<article><div class="row"><span id="number" class="tag"></span><span id="state" class="tag"></span></div>
<h2 id="title"></h2><div id="company" class="muted"></div><p><a id="link" target="_blank" rel="noreferrer">Открыть вакансию на HH ↗</a></p>
<p><b>Заявленная зарплата:</b> <span id="salary"></span><br><b>Формат:</b> <span id="format"></span></p>
<h3>Текст вакансии из RSS</h3><pre id="rss"></pre><p class="muted">Полные обязанности, требования, оклад и KPI в RSS обычно отсутствуют. Для их проверки открой карточку HH. Неизвестные условия не считаются подтверждёнными.</p>
<h3>Предварительная оценка</h3><ul id="pros"></ul><h3>На что обратить внимание</h3><ul id="risks"></ul><h3>Что уточнить</h3><ul id="questions"></ul>
<label for="comment">Комментарий для калибровки / причина отказа</label><textarea id="comment" placeholder="Например: холодные звонки, разъезды 50%, оклад 50 тыс., не подходит продукт"></textarea>
<div class="actions"><button class="approve" onclick="decide('APPROVED')">✅ В отклики</button><button class="reject" onclick="decide('REJECTED')">❌ Отказ</button><button onclick="decide('LATER')">⏳ Пока пропустить</button></div>
<div id="msg"></div></article>
<script>
let jobs=[],pos=0;
function el(id){return document.getElementById(id)}
function list(id,x){el(id).replaceChildren(...x.map(v=>{let li=document.createElement('li');li.textContent=v;return li}))}
function show(){let v=jobs[pos];if(!v)return;
el('progress').textContent=jobs.filter(x=>x.decision.decision).length+' обработано из '+jobs.length;
el('number').textContent='Вакансия '+(pos+1)+' из '+jobs.length;
el('state').textContent=({APPROVED:'✅ В откликах',REJECTED:'❌ Отказ',LATER:'⏳ На потом'})[v.decision.decision]||'Не рассмотрена';
el('title').textContent=v.title;el('company').textContent=v.company;el('salary').textContent=v.salary||'Не указана';
el('format').textContent=v.format;el('link').href=v.url;
el('rss').textContent=v.rss||'В сохранённой записи нет полного текста. Открой вакансию на HH.';
list('pros',v.pros.length?v.pros:['По одному названию нельзя надёжно оценить соответствие.']);
list('risks',v.risks);list('questions',v.questions);
el('comment').value=v.decision.comment||'';el('msg').textContent='';
history.replaceState(null,'','#'+(pos+1))}
function step(n){pos=(pos+n+jobs.length)%jobs.length;show()}
async function decide(decision){let v=jobs[pos],comment=el('comment').value;el('msg').textContent='Сохраняю…';
try{let r=await fetch('/decision',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({id:v.id,decision,comment})});
let j=await r.json();if(!r.ok)throw Error(j.error||'Ошибка сервера');
v.decision={decision,comment};if(pos<jobs.length-1)step(1);else show();
el('msg').textContent='Сохранено в Google Sheets ✅'}catch(e){el('msg').textContent='Не сохранилось: '+e.message}}
fetch('/data').then(r=>r.json()).then(d=>{if(!Array.isArray(d))throw Error(d.error||'Сбой');jobs=d;pos=Math.max(0,Math.min(jobs.length-1,(parseInt(location.hash.slice(1),10)||1)-1));show()}).catch(e=>el('progress').textContent='Ошибка: '+e.message);
</script></body></html>"""

class Handler(BaseHTTPRequestHandler):
    def send(self,code,data,kind):
        blob=data.encode("utf-8");self.send_response(code);self.send_header("Content-Type",kind);self.send_header("Content-Length",str(len(blob)));self.send_header("Cache-Control","no-store");self.end_headers();self.wfile.write(blob)
    def do_GET(self):
        try:
            if urlparse(self.path).path=="/data": self.send(200,json.dumps(read_data(),ensure_ascii=False),"application/json; charset=utf-8")
            elif self.path in ("/","/index.html"): self.send(200,PAGE,"text/html; charset=utf-8")
            else: self.send(404,"Not found","text/plain")
        except Exception as e:self.send(500,json.dumps({"error":str(e)},ensure_ascii=False),"application/json")
    def do_POST(self):
        if self.path!="/decision":return self.send(404,"Not found","text/plain")
        try:
            n=int(self.headers.get("Content-Length","0"))
            if n>12000:raise ValueError("Comment too long")
            j=json.loads(self.rfile.read(n))
            save_decision(str(j.get("id")),str(j.get("decision")),str(j.get("comment",""))[:4000])
            self.send(200,'{"ok":true}',"application/json")
        except Exception as e:self.send(400,json.dumps({"error":str(e)},ensure_ascii=False),"application/json")
if __name__=="__main__":
    port=int(os.getenv("SALES_REVIEW_PORT","8765"))
    print(f"Review UI: http://127.0.0.1:{port}",flush=True)
    ThreadingHTTPServer(("127.0.0.1",port),Handler).serve_forever()
