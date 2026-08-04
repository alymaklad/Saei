"""
Free-tool LLM abstraction.

LLM_PROVIDER=ollama  -> local model via Ollama, $0 forever, needs `ollama serve` running
                         and the model pulled: `ollama pull llama3.1`
LLM_PROVIDER=gemini  -> Google Gemini API free tier, needs GEMINI_API_KEY
LLM_PROVIDER=groq    -> Groq API free tier (fast hosted inference), needs GROQ_API_KEY

Every agent calls get_llm() instead of instantiating a provider directly, so
swapping providers is a one-line .env change.
"""
import config


def get_llm(temperature: float = 0.0):
    if config.LLM_PROVIDER == "gemini":
        from langchain_google_genai import ChatGoogleGenerativeAI
        if not config.GEMINI_API_KEY:
            raise RuntimeError("LLM_PROVIDER=gemini but GEMINI_API_KEY is not set in .env")
        return ChatGoogleGenerativeAI(
            model="gemini-1.5-flash",
            google_api_key=config.GEMINI_API_KEY,
            temperature=temperature,
        )

    if config.LLM_PROVIDER == "groq":
        from langchain_groq import ChatGroq
        if not config.GROQ_API_KEY:
            raise RuntimeError("LLM_PROVIDER=groq but GROQ_API_KEY is not set in .env")
        kwargs = {
            "model": config.GROQ_MODEL,
            "api_key": config.GROQ_API_KEY,
            "temperature": temperature,
            # Without an explicit cap, a long prompt (full CV + job description)
            # can hit Groq's default max_tokens before any actual content is
            # emitted -- see reasoning_effort note below. 4096 leaves plenty of
            # room for a full CV rewrite even after reasoning overhead.
            "max_tokens": 4096,
        }
        # "gpt-oss" models reason internally before producing output, and that
        # reasoning is billed as completion tokens same as the visible content.
        # Verified live: a trivial 2-line rewrite spent ~85% of its completion
        # tokens on hidden reasoning, and a token-capped request came back with
        # content="" + finish_reason="length" (reasoning consumed the whole
        # budget, no error raised). reasoning_effort="low" cuts that overhead
        # so more of the token budget -- and the account's shared daily Groq
        # quota -- goes to the actual CV text instead.
        if "gpt-oss" in config.GROQ_MODEL:
            kwargs["reasoning_effort"] = "low"
        return ChatGroq(**kwargs)

    # default: ollama (local, free)
    from langchain_community.chat_models import ChatOllama
    return ChatOllama(
        model=config.OLLAMA_MODEL,
        base_url=config.OLLAMA_BASE_URL,
        temperature=temperature,
    )
