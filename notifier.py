import os
import smtplib
from email.message import EmailMessage

import requests


def _proxy():
    p = os.getenv("RADAR_PROXY", "").strip()
    return {"http": p, "https": p} if p else None


def _format(project, v):
    emoji = "🔥" if v.get("status") == "TOP" else "🟡"
    subject = f"{emoji} PolyWork: {v.get('status')} — {project['title']}"
    text = (
        f"{emoji} {v.get('status')} — {project['title']}\n\n"
        f"💰 {project.get('budget') or v.get('budget_rub') or 'неясно'}\n"
        f"🤖 Automation: {v.get('automation')}/10\n"
        f"⏱ Human: ~{v.get('human_time_hours')} ч\n"
        f"💬 Communication: {v.get('communication_load')}/10\n"
        f"⚠️ Risk: {v.get('risk')}/10\n"
        f"📈 Effective: {v.get('effective_rub_per_h', 0)} ₽/ч\n\n"
        f"{v.get('reason', '')}\n\n"
        f"Как делать:\n{v.get('agent_plan', '')}\n\n"
        f"Отклик:\n{v.get('reply', '')}\n\n"
        f"🔗 {project['url']}"
    )
    return subject, text


def _chat_id():
    v = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if v:
        return v
    path = os.getenv("TELEGRAM_CHAT_ID_FILE", "").strip()
    if path:
        try:
            return open(path, encoding="utf-8").read().strip()
        except FileNotFoundError:
            pass
    raise RuntimeError("Set TELEGRAM_CHAT_ID or TELEGRAM_CHAT_ID_FILE")


def send_telegram(project, v):
    token = os.getenv("TELEGRAM_TOKEN", "").strip()
    if not token:
        raise RuntimeError("TELEGRAM_TOKEN is empty")
    _, text = _format(project, v)
    r = requests.post(
        "https://api.telegram.org/bot" + token + "/sendMessage",
        json={"chat_id": _chat_id(), "text": text, "disable_web_page_preview": True},
        proxies=_proxy(),
        timeout=30,
    )
    r.raise_for_status()


def send_email(project, v):
    host = os.getenv("SMTP_HOST", "").strip()
    port = int(os.getenv("SMTP_PORT", "465"))
    user = os.getenv("SMTP_USER", "").strip()
    password = os.getenv("SMTP_PASSWORD", "")
    to_addr = os.getenv("EMAIL_TO", "").strip()
    from_addr = os.getenv("EMAIL_FROM", "").strip() or user
    use_ssl = os.getenv("SMTP_SSL", "true").lower() in {"1", "true", "yes", "on"}

    if not host or not user or not password or not to_addr:
        raise RuntimeError("Set SMTP_HOST, SMTP_USER, SMTP_PASSWORD and EMAIL_TO")

    subject, text = _format(project, v)
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = from_addr
    msg["To"] = to_addr
    msg.set_content(text)

    if use_ssl:
        with smtplib.SMTP_SSL(host, port, timeout=30) as smtp:
            smtp.login(user, password)
            smtp.send_message(msg)
    else:
        with smtplib.SMTP(host, port, timeout=30) as smtp:
            smtp.starttls()
            smtp.login(user, password)
            smtp.send_message(msg)


def send(project, v):
    channel = os.getenv("NOTIFY_CHANNEL", "email").strip().lower()
    if channel == "telegram":
        send_telegram(project, v)
    elif channel == "email":
        send_email(project, v)
    elif channel == "both":
        send_email(project, v)
        send_telegram(project, v)
    else:
        raise RuntimeError("NOTIFY_CHANNEL must be email, telegram or both")
