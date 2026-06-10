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

# Fields to fetch from OpenSearch — only what the analyzer pipeline needs.
# Cuts network payload and memory for large result sets.
# Set OPENSEARCH_SOURCE_FIELDS=* to disable projection and fetch all fields.
_raw_source_fields = os.getenv("OPENSEARCH_SOURCE_FIELDS", "@timestamp,container,namespace,message")
OPENSEARCH_SOURCE_FIELDS: list[str] | bool = (
    True  # fetch all
    if _raw_source_fields.strip() == "*"
    else [f.strip() for f in _raw_source_fields.split(",") if f.strip()]
)

# Max log documents passed to the analyzer
MAX_LOG_HITS: int = int(os.getenv("MAX_LOG_HITS", "150"))
# Hard character cap on formatted log text sent to analyzer LLM.
# ~12000 chars ≈ 3000 tokens, leaving room for system prompt + response within 8192 ctx.
# Increase if analyzer model has a larger context window.
MAX_LOG_CHARS: int = int(os.getenv("MAX_LOG_CHARS", "20000"))

PROMPTS_DIR: Path = Path(__file__).parent / "prompts"

# ── Log Deduplication (P2) ─────────────────────────────────────────────────────
LOG_DEDUP_ENABLED: bool = os.getenv("LOG_DEDUP_ENABLED", "true").lower() == "true"
LOG_DEDUP_SIMILARITY_THRESHOLD: float = float(os.getenv("LOG_DEDUP_SIMILARITY_THRESHOLD", "0.85"))
LOG_DEDUP_MAX_LOGS: int = int(os.getenv("LOG_DEDUP_MAX_LOGS", "60"))

# ── Confluence RAG (P7) ────────────────────────────────────────────────────────
# Set CONFLUENCE_ENABLED=true and fill credentials to activate.
# Get an Atlassian API token at: https://id.atlassian.com/manage-profile/security/api-tokens
CONFLUENCE_ENABLED: bool = os.getenv("CONFLUENCE_ENABLED", "false").lower() == "true"
# e.g. https://yourcompany.atlassian.net  (no trailing slash)
CONFLUENCE_SITE_URL: str = os.getenv("CONFLUENCE_SITE_URL", "").rstrip("/")
# Atlassian account email (used with classic API token for REST Basic auth)
CONFLUENCE_USER_EMAIL: str = os.getenv("CONFLUENCE_USER_EMAIL", "")
CONFLUENCE_API_TOKEN: str = os.getenv("CONFLUENCE_API_TOKEN", "")
# ── Rovo MCP token (for MCP server auth at https://mcp.atlassian.com/v1/mcp/authv2)
# Used as Bearer token when connecting to the MCP server.
# Generate at: https://id.atlassian.com/manage-profile/security/api-tokens
CONFLUENCE_MCP_TOKEN: str = os.getenv("CONFLUENCE_MCP_TOKEN", "")
# ── Teamwork Graph token (for getTeamworkGraphContext / getTeamworkGraphObject)
# Separate token dedicated to Teamwork Graph API scope.
# Requires org-admin to enable Teamwork Graph API access at admin.atlassian.com.
# Falls back to CONFLUENCE_MCP_TOKEN if empty.
CONFLUENCE_GRAPH_TOKEN: str = os.getenv("CONFLUENCE_GRAPH_TOKEN", "")
# Comma-separated Confluence space keys to scope searches, e.g. "RUNBOOKS,OPS,PAYMENT"
# Leave empty to search across all spaces the token has access to.
CONFLUENCE_SPACE_KEYS: str = os.getenv("CONFLUENCE_SPACE_KEYS", "")
CONFLUENCE_TOP_K: int = int(os.getenv("CONFLUENCE_TOP_K", "3"))
# Max chars of Confluence content injected per stage (keeps LLM context budget sane)
CONFLUENCE_MAX_CHARS: int = int(os.getenv("CONFLUENCE_MAX_CHARS", "1500"))
# Max LLM tool-calling iterations for the Confluence subagent (complex path)
CONFLUENCE_AGENT_MAX_ITER: int = int(os.getenv("CONFLUENCE_AGENT_MAX_ITER", "5"))
