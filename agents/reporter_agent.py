"""
Reporter Agent — Telegram Bot API (free, unlimited messages).
Replaces the paid Twilio WhatsApp option: Twilio has no real free tier
beyond a small one-time trial credit; a Telegram bot is 100% free forever.

Setup: message @BotFather on Telegram to create a bot and get TELEGRAM_BOT_TOKEN,
then message your bot once and fetch TELEGRAM_CHAT_ID from
https://api.telegram.org/bot<token>/getUpdates
"""
import requests
import config


def send_telegram_report(report_text: str) -> dict:
    if config.DRY_RUN:
        return {"dry_run": True, "action": "send_telegram_report", "text": report_text}

    if not config.TELEGRAM_BOT_TOKEN or not config.TELEGRAM_CHAT_ID:
        return {"error": "TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID not configured"}

    url = f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/sendMessage"
    resp = requests.post(url, json={"chat_id": config.TELEGRAM_CHAT_ID, "text": report_text}, timeout=20)
    resp.raise_for_status()
    return {"dry_run": False, "telegram_response": resp.json()}


def build_daily_report(applications_today: list[dict]) -> str:
    if not applications_today:
        return "No applications submitted today."
    lines = [f"- {a['title']} at {a['company']} ({a['status']})" for a in applications_today]
    return "Today's applications:\n" + "\n".join(lines)
