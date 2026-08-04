"""
Free-tool LLM abstraction.

LLM_PROVIDER=ollama  -> local model via Ollama, $0 forever, needs `ollama serve` running
                         and the model pulled: `ollama pull llama3.1`
LLM_PROVIDER=gemini  -> Google Gemini API free tier, needs GEMINI_API_KEY

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

    # default: ollama (local, free)
    from langchain_community.chat_models import ChatOllama
    return ChatOllama(
        model=config.OLLAMA_MODEL,
        base_url=config.OLLAMA_BASE_URL,
        temperature=temperature,
    )
