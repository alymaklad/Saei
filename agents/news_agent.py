"""News Digest Agent — weekly summary of field news, via free SerpAPI tier + free LLM."""
import requests
from langchain_core.messages import SystemMessage, HumanMessage
from agents.llm import get_llm
import config


def fetch_news(field: str) -> list[dict]:
    if not config.SERPAPI_KEY:
        return []
    resp = requests.get(
        "https://serpapi.com/search",
        params={"engine": "google_news", "q": field, "api_key": config.SERPAPI_KEY},
        timeout=20,
    )
    resp.raise_for_status()
    return resp.json().get("news_results", [])[:15]


def summarize_news(field: str, articles: list[dict]) -> str:
    if not articles:
        return f"No news fetched for '{field}' this week (SERPAPI_KEY not set or no results)."
    llm = get_llm(temperature=0.2)
    titles_snippets = "\n".join(f"- {a.get('title', '')}: {a.get('snippet', '')}" for a in articles)
    messages = [
        SystemMessage(content=f"Summarize this week's most important news for someone in "
                               f"{field}. Group into themes, keep it concise, 5-8 bullet points."),
        HumanMessage(content=titles_snippets),
    ]
    return llm.invoke(messages).content
