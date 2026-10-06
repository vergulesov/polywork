import os, re, requests
from bs4 import BeautifulSoup

BASE='https://www.fl.ru'
LIST=BASE + '/projects/'

def _get(url):
    proxy=os.getenv('RADAR_PROXY','').strip()
    proxies={'http':proxy,'https':proxy} if proxy else None
    r=requests.get(url, headers={'User-Agent':'Mozilla/5.0 PolyWorkRadar'}, proxies=proxies, timeout=int(os.getenv('REQUEST_TIMEOUT','30')))
    r.raise_for_status()
    return r.text

def collect():
    soup=BeautifulSoup(_get(LIST),'html.parser')
    links={}
    for a in soup.find_all('a', href=True):
        m=re.search(r'^/projects/(\d+)/', a['href'])
        if m:
            links.setdefault(m.group(1), BASE + a['href'].split('?')[0])
    out=[]
    limit=int(os.getenv('MAX_PROJECTS_PER_RUN','30'))
    for external_id,url in list(links.items())[:limit]:
        try:
            page=BeautifulSoup(_get(url),'html.parser')
            h1=page.find('h1')
            title=h1.get_text(' ',strip=True) if h1 else 'FL.ru project '+external_id
            text=re.sub(r'\s+',' ',(page.find('main') or page).get_text(' ',strip=True))[:6000]
            money=re.search(r'(?<!\d)(\d[\d\s]{0,12})\s*₽', text)
            budget=money.group(0).strip() if money else ''
            out.append({'source':'FL.ru','external_id':external_id,'title':title,'url':url,'description':text,'budget':budget})
        except Exception as e:
            print('detail error', external_id, type(e).__name__, e)
    return out