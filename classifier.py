import json, os, re, uuid, requests

def _proxy():
    p=os.getenv('RADAR_PROXY','').strip()
    return {'http':p,'https':p} if p else None

def _token():
    r=requests.post(
        os.getenv('GIGACHAT_AUTH_URL','https://ngw.devices.sberbank.ru:9443/api/v2/oauth'),
        headers={'Authorization':'Basic '+os.environ['GIGACHAT_CREDENTIALS'],'RqUID':str(uuid.uuid4()),'Content-Type':'application/x-www-form-urlencoded'},
        data={'scope':os.getenv('GIGACHAT_SCOPE','GIGACHAT_API_PERS')}, proxies=_proxy(), verify=os.getenv('GIGACHAT_VERIFY_SSL','false').lower()=='true', timeout=30)
    r.raise_for_status()
    return r.json()['access_token']

def classify(p):
    prompt=f'''Ты квалификатор фриланс-заказов PolyWork. Ищи простые задачи, где AI/Python/Excel/CMS делает 80-95%, а человек только проверяет.
Приоритет: Excel/Sheets/CSV/XML/YML, WordPress, Drupal, Joomla, 1С-Битрикс, OpenCart, ocStore, наполнение, карточки товаров, SEO title/description/H1/alt, импорт/экспорт, цены/остатки, дедуп, простые SQL.
Не подходят: сложная разработка, дизайн с нуля, звонки, постоянная поддержка, постоянный онлайн, много ручной модерации.
Цель: automation>=8, communication<=3, human_time<=2h, effective>=2000 RUB/h.
Если объём не указан — повышай risk и обычно status=Проверить. Не выдумывай опыт. Отклик короткий, максимум один вопрос.

Название: {p['title']}
Бюджет: {p.get('budget','')}
Описание: {p.get('description','')[:5000]}

Верни только JSON: {{"status":"TOP|Проверить|Не подходит","automation":0,"human_time_hours":0.0,"communication_load":0,"risk":0,"budget_rub":0,"reason":"","agent_plan":"","reply":""}}'''
    r=requests.post(
        os.getenv('GIGACHAT_CHAT_URL','https://gigachat.devices.sberbank.ru/api/v1/chat/completions'),
        headers={'Authorization':'Bearer '+_token(),'Content-Type':'application/json'},
        json={'model':os.getenv('GIGACHAT_MODEL','GigaChat'),'temperature':0.15,'messages':[{'role':'system','content':'Только валидный JSON без markdown.'},{'role':'user','content':prompt}]},
        proxies=_proxy(), verify=os.getenv('GIGACHAT_VERIFY_SSL','false').lower()=='true', timeout=45)
    r.raise_for_status()
    txt=r.json()['choices'][0]['message']['content']
    txt=re.sub(r'```(?:json)?','',txt,flags=re.I).replace('```','').strip()
    data=json.loads(txt[txt.find('{'):txt.rfind('}')+1])
    h=max(float(data.get('human_time_hours') or 0),0)
    b=max(float(data.get('budget_rub') or 0),0)
    data['effective_rub_per_h']=round(b/h) if b and h else 0
    raw_status=str(data.get('status','')).strip()
    if raw_status == 'TOP':
        data['status']='TOP'
    elif raw_status == 'Не подходит':
        data['status']='Не подходит'
    elif raw_status == 'Проверить':
        data['status']='Проверить'
    elif 'Не подходит' in raw_status:
        data['status']='Не подходит'
    elif 'TOP' in raw_status and 'Проверить' in raw_status:
        data['status']='Проверить'
    elif 'TOP' in raw_status:
        data['status']='TOP'
    else:
        data['status']='Проверить'
    return data