"""
OpenSearch client wrapper.
Connection is initialized lazily on first use.
"""

import logging
from typing import Any

from opensearchpy import OpenSearch, exceptions as os_exc

import config

logger = logging.getLogger(__name__)

_client: OpenSearch | None = None


def get_client() -> OpenSearch:
    global _client
    if _client is None:
        kwargs: dict[str, Any] = {
            "hosts": [config.OPENSEARCH_URL],
            "verify_certs": False,
            "ssl_show_warn": False,
        }
        if config.OPENSEARCH_USER and config.OPENSEARCH_PASS:
            kwargs["http_auth"] = (config.OPENSEARCH_USER, config.OPENSEARCH_PASS)
        _client = OpenSearch(**kwargs)
    return _client


def search(query: dict, index: str | None = None) -> list[dict]:
    """
    Execute an OpenSearch query and return a list of _source documents.

    Args:
        query: Full OpenSearch Query DSL body.
        index: Override the default index (config.OPENSEARCH_INDEX).

    Returns:
        List of raw _source dicts.

    Raises:
        RuntimeError: If OpenSearch is unreachable or returns a query error.
    """
    # Inject _source field projection unless the caller already set it.
    # Fetching only the fields the analyzer pipeline consumes dramatically
    # reduces the network payload for documents with many metadata fields.
    body = query
    if "_source" not in query and config.OPENSEARCH_SOURCE_FIELDS is not True:
        body = {**query, "_source": config.OPENSEARCH_SOURCE_FIELDS}
        logger.debug("_source projection applied: %s", config.OPENSEARCH_SOURCE_FIELDS)

    idx = index or config.OPENSEARCH_INDEX
    client = get_client()
    try:
        response = client.search(index=idx, body=body)
    except os_exc.ConnectionError as exc:
        raise RuntimeError(
            f"OpenSearch unreachable at {config.OPENSEARCH_URL}: {exc}"
        ) from exc
    except os_exc.RequestError as exc:
        raise RuntimeError(f"OpenSearch query error: {exc}") from exc

    hits = response.get("hits", {}).get("hits", [])
    logger.info("OpenSearch returned %d hits (index=%s)", len(hits), idx)
    return [hit["_source"] for hit in hits]


def ping() -> bool:
    try:
        return get_client().ping()
    except Exception:
        return False
