import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

# ── LLM (OpenAI-compatible endpoint, e.g. llama-swap, LM Studio, vLLM) ────────
LLM_BASE_URL: str = os.getenv("LLM_BASE_URL", "http://localhost:1234/v1")
LLM_API_KEY: str = os.getenv("LLM_API_KEY", "llm-key")

# Orchestrator: stronger model for intent understanding + query building + final synthesis
ORCHESTRATOR_MODEL: str = os.getenv("ORCHESTRATOR_MODEL", "qwen/qwen3-14b")

# Analyzer: same or lighter model for log execution + data analysis
# Can point to a different base URL if needed for load balancing
ANALYZER_MODEL: str = os.getenv("ANALYZER_MODEL", os.getenv("ORCHESTRATOR_MODEL", "qwen/qwen3-14b"))
ANALYZER_BASE_URL: str = os.getenv("ANALYZER_BASE_URL", os.getenv("LLM_BASE_URL", "http://localhost:1234/v1"))
ANALYZER_API_KEY: str = os.getenv("ANALYZER_API_KEY", os.getenv("LLM_API_KEY", "llm-key"))

# ── OpenSearch ─────────────────────────────────────────────────────────────────
OPENSEARCH_URL: str = os.getenv("OPENSEARCH_URL", "http://localhost:9200")
OPENSEARCH_INDEX: str = os.getenv("OPENSEARCH_INDEX", "payment-platform-uat-sample")
OPENSEARCH_USER: str = os.getenv("OPENSEARCH_USER", "")
OPENSEARCH_PASS: str = os.getenv("OPENSEARCH_PASS", "")

# ── Service ────────────────────────────────────────────────────────────────────
PORT: int = int(os.getenv("PORT", "8080"))

# Max log documents passed to the analyzer
MAX_LOG_HITS: int = int(os.getenv("MAX_LOG_HITS", "150"))
# Hard character cap on formatted log text sent to analyzer LLM.
# ~12000 chars ≈ 3000 tokens, leaving room for system prompt + response within 8192 ctx.
# Increase if analyzer model has a larger context window.
MAX_LOG_CHARS: int = int(os.getenv("MAX_LOG_CHARS", "20000"))

PROMPTS_DIR: Path = Path(__file__).parent / "prompts"
