#!/usr/bin/env python3
"""
Ingest payment log files (JSON format) into OpenSearch.

Expected input: JSON array of log entries in this format:
  [
    {
      "container-name": "ais-payment-submit",
      "timestamp": "2026-06-07T20:33:18.333+07:00",
      "log": { "level": "info", "traceId": "...", ... }
    },
    ...
  ]

The ingested documents will have this structure in OpenSearch:
  {
    "@timestamp": "2026-06-07T20:33:18.333+07:00",
    "container_name": "ais-payment-submit",
    "log": { "level": "info", "traceId": "...", ... }
  }

Usage:
  python3 ingest-payment-logs.py --input ./mock.json
  python3 ingest-payment-logs.py --input ./logs.json --index payment-logs --host http://localhost:9200
"""

import argparse
import json
import sys
from pathlib import Path

try:
    from opensearchpy import OpenSearch, helpers
except ImportError:
    print("ERROR: opensearch-py not installed. Run: pip install opensearch-py")
    sys.exit(1)

INDEX_MAPPINGS = {
    "mappings": {
        "properties": {
            "@timestamp": {"type": "date"},
            "container_name": {"type": "keyword"},
            "log": {
                "properties": {
                    "level": {"type": "keyword"},
                    "type": {"type": "keyword"},
                    "traceId": {"type": "keyword"},
                    "spanId": {"type": "keyword"},
                    "xRequestId": {"type": "keyword"},
                    "xCorrelationId": {"type": "keyword"},
                    "url": {"type": "keyword"},
                    "reqMethod": {"type": "keyword"},
                    "statusCode": {"type": "integer"},
                    "timeInMS": {"type": "long"},
                    "errorCode": {"type": "keyword"},
                    "respErrorCode": {"type": "keyword"},
                    "msg": {"type": "text", "fields": {"keyword": {"type": "keyword", "ignore_above": 512}}},
                    "body": {"type": "text"},
                    "error": {"type": "text"},
                    "size": {"type": "long"},
                    "time": {"type": "keyword"},
                }
            },
        }
    },
    "settings": {
        "number_of_shards": 1,
        "number_of_replicas": 0,
    },
}


def iter_documents(entries: list[dict], index: str):
    for entry in entries:
        # Normalize container-name → container_name
        container = entry.get("container-name") or entry.get("container_name", "")
        timestamp = entry.get("timestamp") or entry.get("@timestamp", "")
        log = entry.get("log", {})

        doc = {
            "@timestamp": timestamp,
            "container_name": container,
            "log": log,
        }
        yield {"_index": index, "_source": doc}


def create_index(client: OpenSearch, index: str):
    if client.indices.exists(index=index):
        print(f"Index '{index}' already exists — appending documents.")
        return
    client.indices.create(index=index, body=INDEX_MAPPINGS)
    print(f"Created index '{index}' with payment log mappings.")


def main():
    parser = argparse.ArgumentParser(description="Ingest payment logs JSON into OpenSearch")
    parser.add_argument("--input", required=True, help="Path to JSON log file")
    parser.add_argument("--index", default="payment-logs", help="OpenSearch index name")
    parser.add_argument("--host", default="http://localhost:9200", help="OpenSearch host URL")
    parser.add_argument("--batch-size", type=int, default=500, help="Bulk batch size")
    parser.add_argument("--recreate", action="store_true", help="Delete and recreate the index")
    args = parser.parse_args()

    input_path = Path(args.input)
    if not input_path.exists():
        print(f"ERROR: input file not found: {input_path}")
        sys.exit(1)

    with open(input_path, encoding="utf-8") as f:
        entries = json.load(f)

    if not isinstance(entries, list):
        print("ERROR: input JSON must be an array of log objects")
        sys.exit(1)

    client = OpenSearch([args.host], verify_certs=False)
    try:
        info = client.info()
        print(f"Connected to OpenSearch {info['version']['number']} at {args.host}")
    except Exception as exc:
        print(f"ERROR: cannot connect to OpenSearch at {args.host}: {exc}")
        sys.exit(1)

    if args.recreate and client.indices.exists(index=args.index):
        client.indices.delete(index=args.index)
        print(f"Deleted existing index '{args.index}'.")

    create_index(client, args.index)

    print(f"Ingesting {len(entries)} log entries...")
    docs = iter_documents(entries, args.index)

    success, failed = 0, 0
    for ok, result in helpers.parallel_bulk(
        client,
        docs,
        chunk_size=args.batch_size,
        raise_on_error=False,
    ):
        if ok:
            success += 1
        else:
            failed += 1
            print(f"  WARN: {result}")

    print(f"\nDone. Indexed: {success:,}  Failed: {failed:,}")
    print(f"Index: {args.index}")
    print(f"Dashboards: http://localhost:5601")


if __name__ == "__main__":
    main()
