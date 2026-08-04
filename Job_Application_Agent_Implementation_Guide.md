# Implementation Guide: Autonomous Job-Search & Application Agent

A multi-agent system that searches for jobs, scores them against your CV, tailors your CV when needed, auto-applies on whitelisted sites, drafts applications elsewhere for your approval, sends required emails, tracks skill gaps, and reports back daily/weekly.

---

## 1. Design Principles

1. **Whitelist-only auto-submit.** The agent only submits automatically on sites you explicitly whitelist (e.g., a company's Greenhouse or Lever career page). Everywhere else, it prepares a complete draft and waits for your approval.
2. **Everything is logged.** Every job seen, scored, drafted, or applied to is stored — this is what powers your daily/weekly reports.
3. **Deterministic + LLM hybrid scoring.** ATS scoring combines keyword/skill overlap (deterministic, matches how real ATS systems filter) with LLM judgment (context, seniority fit).
4. **Idempotent runs.** The agent should never apply to the same job twice — dedupe on job URL/ID before acting.

---

## 2. Architecture Overview

```
                     ┌─────────────────┐
                     │   Scheduler /    │
                     │  Trigger Layer   │  (cron / manual link / on-demand)
                     └────────┬─────────┘
                              │
                     ┌────────▼─────────┐
                     │   Orchestrator    │  (LangGraph state machine)
                     └────────┬─────────┘
        ┌───────────┬─────────┼─────────┬────────────┬─────────────┐
        ▼           ▼         ▼         ▼            ▼             ▼
   Search       ATS Score  CV Rewrite  Apply       Email        Reporter
   Agent        Agent      Agent       Agent       Agent        Agent
        │           │         │         │            │             │
        └─────────────────────┴─────────┴────────────┴─────────────┘
                              │
                     ┌────────▼─────────┐
                     │  SQLite Database  │
                     └───────────────────┘
```

---

## 3. Tech Stack

| Component | Tool | Why |
|---|---|---|
| Orchestration | **LangGraph** | Explicit branching (score high/low, whitelisted/not) fits a state graph better than a linear chain |
| LLM | Claude or GPT-4o via API | CV rewriting, scoring judgment, summarization |
| Job search | SerpAPI (Google Jobs), Greenhouse/Lever public job APIs, site-specific scraping only where ToS allows | Greenhouse (`boards-api.greenhouse.io`) and Lever (`api.lever.co`) both expose public JSON job listings — ideal for reliable automation |
| Watchlist input | Google Sheets (`gspread`) | A sheet where you manually list companies/roles of interest — the agent reads it as an extra search input, covering the project's "Google Sheets" data source requirement |
| Page scraping (non-whitelisted) | `requests` + `BeautifulSoup`; Playwright only if JS-rendered and permitted | Avoid scraping sites whose ToS forbids it |
| CV parsing | `pdfplumber` (PDF) / `python-docx` (Word) | |
| CV rewriting output | `python-docx` template | Keeps formatting consistent |
| Email | Gmail API (OAuth) or SendGrid | Sending application emails |
| WhatsApp | Twilio WhatsApp Business API | Daily report delivery |
| Scheduling | APScheduler (in-process) or system `cron` | Daily/weekly triggers |
| Storage | SQLite via `sqlalchemy` | Lightweight, file-based, easy to inspect |
| Secrets | `.env` + `python-dotenv` | Never hardcode keys |

Install:

```bash
pip install langgraph langchain langchain-openai langchain-anthropic \
    pdfplumber python-docx requests beautifulsoup4 playwright \
    sqlalchemy python-dotenv google-api-python-client google-auth-oauthlib \
    twilio sendgrid apscheduler gspread google-auth
playwright install chromium
```

---

## 4. Database Schema

```python
# models.py
from sqlalchemy import Column, Integer, String, Float, Text, DateTime, Boolean, ForeignKey
from sqlalchemy.orm import declarative_base
from datetime import datetime

Base = declarative_base()

class Job(Base):
    __tablename__ = "jobs"
    id = Column(Integer, primary_key=True)
    url = Column(String, unique=True, nullable=False)
    title = Column(String)
    company = Column(String)
    source_site = Column(String)          # "greenhouse", "lever", "manual_link", etc.
    description = Column(Text)
    is_whitelisted_source = Column(Boolean, default=False)
    date_found = Column(DateTime, default=datetime.utcnow)

class Application(Base):
    __tablename__ = "applications"
    id = Column(Integer, primary_key=True)
    job_id = Column(Integer, ForeignKey("jobs.id"))
    ats_score = Column(Float)
    cv_version_path = Column(String)
    status = Column(String)               # "scored_low", "drafted", "auto_submitted", "pending_review", "sent"
    email_sent = Column(Boolean, default=False)
    date_applied = Column(DateTime, nullable=True)
    date_created = Column(DateTime, default=datetime.utcnow)

class SkillGap(Base):
    __tablename__ = "skill_gaps"
    id = Column(Integer, primary_key=True)
    job_id = Column(Integer, ForeignKey("jobs.id"))
    missing_skills = Column(Text)         # JSON list
    date_created = Column(DateTime, default=datetime.utcnow)

class NewsDigest(Base):
    __tablename__ = "news_digests"
    id = Column(Integer, primary_key=True)
    field = Column(String)
    content = Column(Text)
    date_created = Column(DateTime, default=datetime.utcnow)
```

---

## 5. Agent Implementations

### 5.1 Search Agent (Feature 1, 5)

```python
# agents/search_agent.py
import requests

def search_greenhouse(board_token: str) -> list[dict]:
    url = f"https://boards-api.greenhouse.io/v1/boards/{board_token}/jobs?content=true"
    resp = requests.get(url)
    resp.raise_for_status()
    return resp.json().get("jobs", [])

def search_lever(company: str) -> list[dict]:
    url = f"https://api.lever.co/v0/postings/{company}?mode=json"
    resp = requests.get(url)
    resp.raise_for_status()
    return resp.json()

def search_serpapi(query: str, api_key: str) -> list[dict]:
    resp = requests.get("https://serpapi.com/search", params={
        "engine": "google_jobs", "q": query, "api_key": api_key
    })
    resp.raise_for_status()
    return resp.json().get("jobs_results", [])

def read_watchlist_sheet(sheet_id: str, creds_path: str) -> list[dict]:
    """
    Reads a Google Sheet where the user manually lists companies/roles of interest.
    Expected columns: company | role_keyword | greenhouse_board_token | lever_company_slug
    Rows can leave the board/slug columns blank if the company isn't on Greenhouse/Lever —
    in that case the row is used as a search_serpapi() query instead.
    """
    import gspread
    from google.oauth2.service_account import Credentials

    creds = Credentials.from_service_account_file(
        creds_path,
        scopes=["https://www.googleapis.com/auth/spreadsheets.readonly"]
    )
    client = gspread.authorize(creds)
    sheet = client.open_by_key(sheet_id).sheet1
    return sheet.get_all_records()

def search_from_watchlist(sheet_id: str, creds_path: str, serpapi_key: str) -> list[dict]:
    results = []
    for row in read_watchlist_sheet(sheet_id, creds_path):
        board = row.get("greenhouse_board_token")
        slug = row.get("lever_company_slug")
        if board:
            results += [{"source": "greenhouse", "watchlist_company": row.get("company"), **j}
                        for j in search_greenhouse(board)]
        elif slug:
            results += [{"source": "lever", "watchlist_company": row.get("company"), **j}
                        for j in search_lever(slug)]
        else:
            query = f"{row.get('role_keyword', '')} {row.get('company', '')}".strip()
            results += [{"source": "google_jobs", "watchlist_company": row.get("company"), **j}
                        for j in search_serpapi(query, serpapi_key)]
    return results

WHITELISTED_SOURCES = {"greenhouse", "lever"}  # user-configurable

def run_search(sources: list[str], query: str, serpapi_key: str,
                sheet_id: str = None, sheet_creds_path: str = None) -> list[dict]:
    results = []
    for src in sources:
        if src == "greenhouse_boards":
            for board in ["stripe", "airbnb"]:  # user-configured board tokens
                results += [{"source": "greenhouse", **j} for j in search_greenhouse(board)]
        elif src == "lever_companies":
            for company in ["netflix"]:  # user-configured
                results += [{"source": "lever", **j} for j in search_lever(company)]
        elif src == "google_jobs":
            results += [{"source": "google_jobs", **j} for j in search_serpapi(query, serpapi_key)]
        elif src == "watchlist_sheet":
            if sheet_id and sheet_creds_path:
                results += search_from_watchlist(sheet_id, sheet_creds_path, serpapi_key)
    return results
```

### 5.2 ATS Scoring Agent (Feature 1, 2, 5)

```python
# agents/ats_agent.py
import json
from langchain_openai import ChatOpenAI
from langchain_core.messages import SystemMessage, HumanMessage

llm = ChatOpenAI(model="gpt-4o", temperature=0)

def extract_required_skills(job_description: str) -> list[str]:
    messages = [
        SystemMessage(content="Extract the required and preferred skills/keywords from this "
                               "job description as a JSON list of strings only."),
        HumanMessage(content=job_description)
    ]
    resp = llm.invoke(messages)
    try:
        return json.loads(resp.content)
    except json.JSONDecodeError:
        return []

def keyword_overlap_score(cv_text: str, required_skills: list[str]) -> float:
    cv_lower = cv_text.lower()
    if not required_skills:
        return 0.0
    matched = sum(1 for s in required_skills if s.lower() in cv_lower)
    return matched / len(required_skills)

def llm_fit_score(cv_text: str, job_description: str) -> float:
    messages = [
        SystemMessage(content="You are an ATS/recruiter evaluator. Given a CV and a job "
                               "description, return ONLY a number from 0 to 1 representing "
                               "overall fit (experience level, domain relevance, skills)."),
        HumanMessage(content=f"CV:\n{cv_text}\n\nJob Description:\n{job_description}")
    ]
    resp = llm.invoke(messages)
    try:
        return float(resp.content.strip())
    except ValueError:
        return 0.5

def compute_ats_score(cv_text: str, job_description: str) -> dict:
    required_skills = extract_required_skills(job_description)
    kw_score = keyword_overlap_score(cv_text, required_skills)
    llm_score = llm_fit_score(cv_text, job_description)
    final_score = 0.5 * kw_score + 0.5 * llm_score
    missing = [s for s in required_skills if s.lower() not in cv_text.lower()]
    return {"score": final_score, "missing_skills": missing, "required_skills": required_skills}
```

### 5.3 CV Rewriter Agent (Feature 2)

```python
# agents/cv_rewriter_agent.py
from langchain_openai import ChatOpenAI
from langchain_core.messages import SystemMessage, HumanMessage
from docx import Document

llm = ChatOpenAI(model="gpt-4o", temperature=0.3)

def rewrite_cv(cv_text: str, job_description: str, missing_skills: list[str]) -> str:
    messages = [
        SystemMessage(content="Rewrite this CV to be ATS-optimized for the given job description. "
                               "Naturally incorporate relevant keywords the candidate genuinely has "
                               "experience with, restructure bullet points around measurable impact, "
                               "and do not fabricate skills or experience the candidate doesn't have. "
                               "Flag skills the candidate is missing separately, don't invent them into the CV."),
        HumanMessage(content=f"Current CV:\n{cv_text}\n\nJob Description:\n{job_description}\n\n"
                              f"Known missing skills (do NOT fabricate these into the CV):\n{missing_skills}")
    ]
    resp = llm.invoke(messages)
    return resp.content

def save_cv_as_docx(cv_text: str, output_path: str):
    doc = Document()
    for line in cv_text.split("\n"):
        doc.add_paragraph(line)
    doc.save(output_path)
```

> **Integrity note:** the prompt explicitly instructs the model not to fabricate skills/experience — ATS-optimization should mean better *framing* of real experience, not lying on your CV.

### 5.4 Apply Agent (Feature 1, 4) — Whitelist Logic

```python
# agents/apply_agent.py
WHITELISTED_ATS_PLATFORMS = {"greenhouse", "lever"}  # only these auto-submit

def decide_apply_path(job: dict) -> str:
    if job["source"] in WHITELISTED_ATS_PLATFORMS:
        return "auto_submit"
    return "draft_for_review"

def auto_submit_greenhouse(job_url: str, cv_path: str, cover_letter: str, applicant_info: dict):
    # Greenhouse job board postings often expose an application POST endpoint.
    # Exact fields vary per posting — inspect the specific board's application form schema
    # (via its public API) before wiring this up, and test on a throwaway application first.
    raise NotImplementedError("Wire up per-board application schema after inspecting the target board")

def draft_for_review(job: dict, cv_path: str, cover_letter: str) -> dict:
    return {
        "job_url": job["url"] if "url" in job else job.get("hostedUrl"),
        "cv_path": cv_path,
        "cover_letter": cover_letter,
        "status": "pending_review"
    }
```

**Why `auto_submit_greenhouse` is left as a stub:** every Greenhouse/Lever job board can have different custom application fields (some require screening questions, EEO fields, etc.). Before enabling true auto-submit, inspect the specific board's form schema via browser dev tools or the API, test against one real job end-to-end, and confirm the payload works — this is the one part of the system you should hand-verify per employer.

### 5.5 Email Agent (Feature 3)

```python
# agents/email_agent.py
import base64
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.application import MIMEApplication
from googleapiclient.discovery import build

def send_application_email(gmail_service, to_email: str, subject: str, body: str, cv_path: str):
    message = MIMEMultipart()
    message["to"] = to_email
    message["subject"] = subject
    message.attach(MIMEText(body))

    with open(cv_path, "rb") as f:
        part = MIMEApplication(f.read(), Name="CV.docx")
    part["Content-Disposition"] = f'attachment; filename="CV.docx"'
    message.attach(part)

    raw = base64.urlsafe_b64encode(message.as_bytes()).decode()
    gmail_service.users().messages().send(userId="me", body={"raw": raw}).execute()
```

### 5.6 Skill Gap Agent (Feature 5)

```python
# agents/skill_gap_agent.py
def summarize_skill_gaps(job_scores: list[dict]) -> str:
    from collections import Counter
    all_missing = []
    for j in job_scores:
        all_missing.extend(j["missing_skills"])
    counts = Counter(all_missing)
    top_gaps = counts.most_common(10)
    lines = [f"- {skill}: missing in {count} of {len(job_scores)} jobs reviewed" for skill, count in top_gaps]
    return "Top skill gaps for your field:\n" + "\n".join(lines)
```

### 5.7 News Digest Agent (Feature 6)

```python
# agents/news_agent.py
from langchain_openai import ChatOpenAI
from langchain_core.messages import SystemMessage, HumanMessage
import requests

llm = ChatOpenAI(model="gpt-4o", temperature=0.2)

def fetch_news(field: str, api_key: str) -> list[dict]:
    resp = requests.get("https://serpapi.com/search", params={
        "engine": "google_news", "q": field, "api_key": api_key
    })
    resp.raise_for_status()
    return resp.json().get("news_results", [])[:15]

def summarize_news(field: str, articles: list[dict]) -> str:
    titles_snippets = "\n".join(f"- {a['title']}: {a.get('snippet','')}" for a in articles)
    messages = [
        SystemMessage(content=f"Summarize this week's most important news for someone in {field}. "
                               "Group into themes, keep it concise, 5-8 bullet points."),
        HumanMessage(content=titles_snippets)
    ]
    return llm.invoke(messages).content
```

### 5.8 Reporter Agent (Feature 7)

```python
# agents/reporter_agent.py
from twilio.rest import Client

def send_whatsapp_report(twilio_sid: str, twilio_token: str, from_whatsapp: str,
                          to_whatsapp: str, report_text: str):
    client = Client(twilio_sid, twilio_token)
    client.messages.create(from_=from_whatsapp, to=to_whatsapp, body=report_text)

def build_daily_report(applications_today: list[dict]) -> str:
    if not applications_today:
        return "No applications submitted today."
    lines = [f"- {a['title']} at {a['company']} ({a['status']})" for a in applications_today]
    return "Today's applications:\n" + "\n".join(lines)
```

---

## 6. Orchestrator (LangGraph)

```python
# orchestrator.py
from langgraph.graph import StateGraph, END
from typing import TypedDict, Optional

class State(TypedDict):
    job: dict
    cv_text: str
    ats_result: dict
    cv_rewritten: Optional[str]
    apply_path: str
    status: str

def score_node(state: State) -> State:
    from agents.ats_agent import compute_ats_score
    state["ats_result"] = compute_ats_score(state["cv_text"], state["job"]["description"])
    return state

def route_on_score(state: State) -> str:
    return "rewrite_cv" if state["ats_result"]["score"] < 0.7 else "decide_apply_path"

def rewrite_node(state: State) -> State:
    from agents.cv_rewriter_agent import rewrite_cv
    state["cv_rewritten"] = rewrite_cv(
        state["cv_text"], state["job"]["description"], state["ats_result"]["missing_skills"]
    )
    state["status"] = "cv_rewritten_notify_user"
    return state

def decide_apply_path_node(state: State) -> State:
    from agents.apply_agent import decide_apply_path
    state["apply_path"] = decide_apply_path(state["job"])
    return state

def route_on_apply_path(state: State) -> str:
    return state["apply_path"]  # "auto_submit" or "draft_for_review"

def auto_submit_node(state: State) -> State:
    state["status"] = "auto_submitted"
    return state

def draft_node(state: State) -> State:
    state["status"] = "pending_review"
    return state

graph = StateGraph(State)
graph.add_node("score", score_node)
graph.add_node("rewrite_cv", rewrite_node)
graph.add_node("decide_apply_path", decide_apply_path_node)
graph.add_node("auto_submit", auto_submit_node)
graph.add_node("draft_for_review", draft_node)

graph.set_entry_point("score")
graph.add_conditional_edges("score", route_on_score, {
    "rewrite_cv": "rewrite_cv", "decide_apply_path": "decide_apply_path"
})
graph.add_edge("rewrite_cv", END)  # notify user, stop — don't auto-apply with a low-fit CV
graph.add_conditional_edges("decide_apply_path", route_on_apply_path, {
    "auto_submit": "auto_submit", "draft_for_review": "draft_for_review"
})
graph.add_edge("auto_submit", END)
graph.add_edge("draft_for_review", END)

app = graph.compile()
```

---

## 7. Scheduling

```python
# scheduler.py
from apscheduler.schedulers.blocking import BlockingScheduler
from jobs.daily_run import run_daily_search_and_apply
from jobs.weekly_news import run_weekly_news_digest
from jobs.daily_report import run_daily_report

scheduler = BlockingScheduler()
scheduler.add_job(run_daily_search_and_apply, "cron", hour=8)
scheduler.add_job(run_daily_report, "cron", hour=20)
scheduler.add_job(run_weekly_news_digest, "cron", day_of_week="mon", hour=9)

if __name__ == "__main__":
    scheduler.start()
```

---

## 8. Manual Link Application (Feature 4)

```python
# jobs/apply_from_link.py
from agents.search_agent import search_greenhouse  # or generic scraper
import requests
from bs4 import BeautifulSoup
from orchestrator import app

def apply_from_link(url: str, cv_text: str):
    resp = requests.get(url)
    soup = BeautifulSoup(resp.text, "html.parser")
    description = soup.get_text(separator="\n")
    source = "greenhouse" if "greenhouse.io" in url else ("lever" if "lever.co" in url else "manual_link")

    job = {"url": url, "description": description, "source": source, "title": soup.title.string if soup.title else url}
    result = app.invoke({"job": job, "cv_text": cv_text})
    return result
```

---

## 9. Testing & Evaluation

1. **Unit tests** per agent (scoring returns 0–1, CV rewriter doesn't fabricate skills — spot-check output against known missing skills list).
2. **Whitelist enforcement test** — assert `decide_apply_path` never returns `auto_submit` for a non-whitelisted source, even adversarially crafted job dicts.
3. **Dedup test** — running search twice on the same job list should not create duplicate `Application` rows.
4. **Dry run mode** — add a global `DRY_RUN=True` flag that logs intended actions (send email, submit application) without actually performing them, for safe end-to-end testing.
5. **Manual review of first N real applications** before trusting the whitelist auto-submit path in production.

---

## 10. Suggested Build Order

| Phase | What to build |
|---|---|
| 1 | DB schema + CV parsing + ATS scoring agent |
| 2 | Search agent (Greenhouse/Lever APIs first — reliable, ToS-friendly) |
| 3 | Google Sheets watchlist integration (`read_watchlist_sheet`) |
| 4 | CV rewriter agent + docx export |
| 5 | Draft-for-review apply flow (no auto-submit yet) |
| 6 | Email agent |
| 7 | Orchestrator wiring everything together, dry-run mode |
| 8 | Reporter agent (WhatsApp/email daily report) |
| 9 | News digest agent (weekly) |
| 10 | Enable auto-submit for one whitelisted board after manual verification |

---

## 11. Key Cautions

- **Never fabricate CV content** — the rewriter must reframe real experience, not invent skills.
- **Respect job board Terms of Service** — stick to public APIs (Greenhouse, Lever) or explicit permission; avoid scraping sites that prohibit it.
- **Rate-limit your searches/applications** — sending too many automated requests can get your IP or account flagged.
- **Review before scaling auto-submit** — start with draft-for-review everywhere, and only whitelist a board after you've manually verified a submission works correctly on it.
