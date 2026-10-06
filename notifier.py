import os, requests

def _proxy():
    p=os.getenv('RADAR_PROXY','').strip()
    return {'http':p,'https':p} if p else None

def _chat_id():
    v=os.getenv('TELEGRAM_CHAT_ID','').strip()
    if v: return v
    path=os.getenv('TELEGRAM_CHAT_ID_FILE','').strip()
    if path:
        try:
            return open(path,encoding='utf-8').read().strip()
        except FileNotFoundError:
            pass
    raise RuntimeError('Set TELEGRAM_CHAT_ID or TELEGRAM_CHAT_ID_FILE')

def send(project,v):
    emoji='🔥' if v.get('status')=='TOP' else '🟡'
    text=(f"{emoji} {v.get('status')} — {project['title']}\n\n"
          f"💰 {project.get('budget') or v.get('budget_rub') or 'неясно'}\n"
          f"🤖 Automation: {v.get('automation')}/10\n"
          f"⏱ Human: ~{v.get('human_time_hours')} ч\n"
          f"💬 Communication: {v.get('communication_load')}/10\n"
          f"⚠️ Risk: {v.get('risk')}/10\n"
          f"📈 Effective: {v.get('effective_rub_per_h',0)} ₽/ч\n\n"
          f"{v.get('reason','')}\n\nКак делать:\n{v.get('agent_plan','')}\n\nОтклик:\n{v.get('reply','')}\n\n🔗 {project['url']}")
    r=requests.post('https://api.telegram.org/bot'+os.environ['TELEGRAM_TOKEN']+'/sendMessage',json={'chat_id':_chat_id(),'text':text,'disable_web_page_preview':True},proxies=_proxy(),timeout=30)
    r.raise_for_status()