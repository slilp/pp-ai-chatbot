---
name: project-arch
description: Architecture of the pp-ai-chatbot project — services, data flow, and multi-agent design
metadata:
  type: project
---

## Project: KTB Payment Log AI Chatbot

**Why:** Help payment engineers investigate transaction errors and traces via natural language.

### Services
- `ai-service-py/` — Python FastAPI multi-agent AI service (successor to `ai-service/` Go service)
- `chatbot-ui/` — Next.js chat frontend (SSE streaming)
- OpenSearch — payment log store

### Multi-Agent Architecture (`ai-service-py/`)
Two Anthropic SDK agents:
1. **Orchestrator** (`agents/orchestrator.py`) — `claude-sonnet-4-6` (configurable via `ORCHESTRATOR_MODEL`)
   - Understands user intent, builds OpenSearch query DSL via tool_use
   - Never sees raw log data (context protected)
   - Streams final Thai-language response to user
2. **Analyzer** (`agents/analyzer.py`) — `claude-haiku-4-5-20251001` (configurable via `ANALYZER_MODEL`)
   - Executes OpenSearch query, masks PII, compresses logs
   - Returns structured analysis markdown back to orchestrator

### SSE Protocol (frontend ↔ backend)
- `data: "token"` — JSON-encoded text token
- `data: {"type":"status","message":"..."}` — progress update
- `data: [DONE]` — stream complete
- `data: [ERROR] message` — error

### Prompts (easily editable)
All in `ai-service-py/prompts/`:
- `business_context.md` — domain knowledge (error codes, service names, log fields)
- `system_orchestrator.md` — orchestrator instructions + OpenSearch query examples
- `system_analyzer.md` — analyzer instructions + output format

### OpenSearch Index (`payment-logs`)
Documents: `{ "@timestamp", "container_name", "log.{level,traceId,type,url,statusCode,...}" }`
Ingest script: `opensearch/ingest-payment-logs.py --input <json-file>`

**How to apply:** When adding new payment services or error codes, update `business_context.md`. To change query behavior, update `system_orchestrator.md`. To change analysis format, update `system_analyzer.md`.
