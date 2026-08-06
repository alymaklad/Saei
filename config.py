"""
Central configuration. All values come from environment variables (.env),
so no secrets ever live in code. Copy .env.example to .env and fill it in.
"""
import os
from dotenv import load_dotenv

load_dotenv()


def _bool(name: str, default: bool = False) -> bool:
    return os.getenv(name, str(default)).strip().lower() in ("1", "true", "yes", "on")


def _list(name: str) -> list[str]:
    raw = os.getenv(name, "")
    return [item.strip() for item in raw.split(",") if item.strip()]


def _int_or_none(name: str) -> int | None:
    raw = os.getenv(name, "").strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _seeded_template_keys(name: str) -> set[str]:
    """
    Which agents.search_agent.KNOWN_JOB_BOARD_TEMPLATES keys have already
    been seeded into the search_sites table. Stored as a comma-separated
    list of keys (e.g. "bayt,wuzzuf") rather than a plain bool, so that
    adding a NEW template later (e.g. adding "remoteok" to the dict) still
    gets seeded on the next run for existing installs, without silently
    re-adding a template the user deliberately removed.

    Migrates the old boolean format transparently: this flag used to be a
    plain true/false before per-template tracking existed, back when
    KNOWN_JOB_BOARD_TEMPLATES only had "wuzzuf" and "bayt" -- so a legacy
    "true" value means exactly {"wuzzuf", "bayt"} were seeded, not "every
    template that will ever exist."
    """
    raw = os.getenv(name, "").strip()
    if not raw:
        return set()
    if raw.lower() in ("true", "1", "yes", "on"):
        return {"wuzzuf", "bayt"}
    if raw.lower() in ("false", "0", "no", "off"):
        return set()
    return {item.strip() for item in raw.split(",") if item.strip()}


# LLM
LLM_PROVIDER = os.getenv("LLM_PROVIDER", "ollama")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "llama3.1")
OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
GROQ_MODEL = os.getenv("GROQ_MODEL", "openai/gpt-oss-120b")

# Job search
SERPAPI_KEY = os.getenv("SERPAPI_KEY", "")
GREENHOUSE_BOARD_TOKENS = _list("GREENHOUSE_BOARD_TOKENS")
LEVER_COMPANY_SLUGS = _list("LEVER_COMPANY_SLUGS")

# Default position/title to search for -- used by the Search tab's "search
# now" button and by the scheduler's automatic 8am run alike, so both stay in
# sync with whatever the user last saved. Blank means "no title filter".
SEARCH_POSITION_QUERY = os.getenv("SEARCH_POSITION_QUERY", "")

# Seniority filter: one of "" (any), "intern", "entry", "mid", "senior",
# "lead", "manager". Same sync behavior as SEARCH_POSITION_QUERY above.
SEARCH_SENIORITY_LEVEL = os.getenv("SEARCH_SENIORITY_LEVEL", "")

# Tracks which agents.search_agent.KNOWN_JOB_BOARD_TEMPLATES keys have
# already been seeded into search_sites, so seed_default_search_sites()
# knows what's left to add without re-adding anything the user removed by
# hand. See _seeded_template_keys() above for the legacy-bool migration.
SEARCH_DEFAULT_SITES_SEEDED = _seeded_template_keys("SEARCH_DEFAULT_SITES_SEEDED")

# Max raw results kept from each individual source (each Greenhouse board,
# Lever company, watchlist row, or added website) before filtering. Blank/0 =
# no limit -- preserves the original "pull everything" behavior. Generic
# scraped sites are already capped internally (see GENERIC_SITE_MAX_CANDIDATES
# in agents/search_agent.py) even when this is unset, since each candidate
# there costs a real HTTP fetch.
SEARCH_MAX_RESULTS_PER_SITE = _int_or_none("SEARCH_MAX_RESULTS_PER_SITE")

# Max age in days for a job posting to be included. Blank/0 = no limit. Only
# applied where a posted date can actually be determined (Greenhouse, Lever,
# SerpAPI/Google Jobs) -- sources with no reliable date (generic scraped
# sites) are never excluded by this filter since their age can't be verified.
SEARCH_MAX_AGE_DAYS = _int_or_none("SEARCH_MAX_AGE_DAYS")

# Google Sheets watchlist
GOOGLE_SHEETS_ID = os.getenv("GOOGLE_SHEETS_ID", "")
GOOGLE_SERVICE_ACCOUNT_JSON = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "credentials/service_account.json")

# Gmail
GMAIL_CREDENTIALS_JSON = os.getenv("GMAIL_CREDENTIALS_JSON", "credentials/gmail_credentials.json")
GMAIL_TOKEN_JSON = os.getenv("GMAIL_TOKEN_JSON", "credentials/gmail_token.json")
APPLICANT_NAME = os.getenv("APPLICANT_NAME", "")
APPLICANT_EMAIL = os.getenv("APPLICANT_EMAIL", "")

# Telegram
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

# Whitelist: entries look like "greenhouse:stripe" or "lever:netflix".
# Empty by default — nothing auto-submits until you explicitly add an entry
# AFTER manually verifying that board's application form works end-to-end.
WHITELISTED_SOURCES = set(_list("WHITELISTED_SOURCES"))

# Safety
DRY_RUN = _bool("DRY_RUN", True)

# Storage
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./data/job_agent.db")
