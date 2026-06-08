"""
Payment Log AI Service — FastAPI entrypoint.

Exposes a single POST /chat endpoint that streams SSE responses.
The endpoint is a drop-in replacement for the previous Go ai-service.
"""

import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator

import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

import config
from agents.orchestrator import orchestrate
from opensearch_client.client import ping

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    if not config.LLM_API_KEY:
        logger.warning("LLM_API_KEY is not set")
    os_ok = ping()
    logger.info(
        "OpenSearch at %s: %s",
        config.OPENSEARCH_URL,
        "reachable" if os_ok else "UNREACHABLE",
    )
    logger.info(
        "Models — orchestrator: %s  analyzer: %s",
        config.ORCHESTRATOR_MODEL,
        config.ANALYZER_MODEL,
    )
    yield


app = FastAPI(title="Payment Log AI Service", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["POST", "GET", "OPTIONS"],
    allow_headers=["*"],
)


class HistoryMessage(BaseModel):
    role: str
    content: str


class ChatRequest(BaseModel):
    question: str
    history: list[HistoryMessage] = []


@app.post("/chat")
async def chat(req: ChatRequest):
    async def event_stream() -> AsyncIterator[str]:
        history = [m.model_dump() for m in req.history]
        async for chunk in orchestrate(req.question, history):
            yield chunk

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
            "Access-Control-Allow-Origin": "*",
        },
    )


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "opensearch": ping(),
        "orchestrator_model": config.ORCHESTRATOR_MODEL,
        "analyzer_model": config.ANALYZER_MODEL,
    }


if __name__ == "__main__":
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=config.PORT,
        log_level="info",
    )
