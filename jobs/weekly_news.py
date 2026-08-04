"""Weekly news digest for the user's field, stored + sent via Telegram."""
from db import get_session, init_db
from models import NewsDigest
from agents.news_agent import fetch_news, summarize_news
from agents.reporter_agent import send_telegram_report

DEFAULT_FIELD = "software engineering"


def run_weekly_news_digest(field: str = DEFAULT_FIELD):
    init_db()
    articles = fetch_news(field)
    summary = summarize_news(field, articles)

    with get_session() as session:
        session.add(NewsDigest(field=field, content=summary))

    send_telegram_report(f"Weekly news digest ({field}):\n\n{summary}")
    return summary


if __name__ == "__main__":
    print(run_weekly_news_digest())
