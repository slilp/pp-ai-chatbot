#!/usr/bin/env python3
"""
Ingest local log files (dumped by dump-k8s-logs.sh) into OpenSearch.

Directory layout expected:
  <logs-dir>/<namespace>/<pod>/<container>.log

Each line becomes one document in OpenSearch with fields:
  namespace, pod, container, timestamp (parsed from kubectl --timestamps), message

Usage:
  python3 ingest-logs.py
  python3 ingest-logs.py --logs-dir ./logs --index k8s-logs --host http://localhost:9200
"""

import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

try:
    from opensearchpy import OpenSearch, helpers
except ImportError:
    print("ERROR: opensearch-py not installed. Run: pip install opensearch-py")
    sys.exit(1)

# kubectl --timestamps format: 2024-01-15T12:34:56.789012345Z <message>
TIMESTAMP_RE = re.compile(
    r'^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z)\s+(.*)', re.DOTALL
)


def parse_line(line: str) -> tuple[str | None, str]:
    m = TIMESTAMP_RE.match(line)
    if m:
        return m.group(1), m.group(2)
    return None, line


def iter_documents(logs_dir: Path, index: str):
    for log_file in sorted(logs_dir.rglob("*.log")):
        relative = log_file.relative_to(logs_dir)
        parts = relative.parts  # (namespace, pod, container.log)
        if len(parts) != 3:
            continue
        namespace, pod, container_file = parts
        container = container_file[:-4]  # strip .log

        with open(log_file, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.rstrip("\n")
                if not line:
                    continue
                ts, msg = parse_line(line)
                doc: dict = {
                    "namespace": namespace,
                    "pod": pod,
                    "container": container,
                    "message": msg,
                    "log_file": str(relative),
                }
                if ts:
                    doc["@timestamp"] = ts
                else:
                    doc["@timestamp"] = datetime.now(timezone.utc).isoformat()

                yield {"_index": index, "_source": doc}


def create_index(client: OpenSearch, index: str):
    if client.indices.exists(index=index):
        print(f"Index '{index}' already exists — appending.")
        return
    mappings = {
        "mappings": {
            "properties": {
                "@timestamp": {"type": "date"},
                "namespace": {"type": "keyword"},
                "pod": {"type": "keyword"},
                "container": {"type": "keyword"},
                "message": {"type": "text"},
                "log_file": {"type": "keyword"},
            }
        }
    }
    client.indices.create(index=index, body=mappings)
    print(f"Created index '{index}'.")


def main():
    parser = argparse.ArgumentParser(description="Ingest K8s logs into OpenSearch")
    parser.add_argument("--logs-dir", default="./logs", help="Root directory of dumped logs")
    parser.add_argument("--index", default="k8s-logs", help="OpenSearch index name")
    parser.add_argument("--host", default="http://localhost:9200", help="OpenSearch host URL")
    parser.add_argument("--batch-size", type=int, default=500, help="Bulk batch size")
    args = parser.parse_args()

    logs_dir = Path(args.logs_dir)
    if not logs_dir.is_dir():
        print(f"ERROR: logs directory not found: {logs_dir}")
        sys.exit(1)

    client = OpenSearch([args.host], verify_certs=False)
    try:
        info = client.info()
        print(f"Connected to OpenSearch {info['version']['number']} at {args.host}")
    except Exception as e:
        print(f"ERROR: cannot connect to OpenSearch at {args.host}: {e}")
        sys.exit(1)

    create_index(client, args.index)

    print(f"Ingesting logs from: {logs_dir}")
    docs = iter_documents(logs_dir, args.index)

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
            print(f"  WARN: failed to index doc: {result}")

    print(f"\nDone. Indexed: {success:,}  Failed: {failed:,}")
    print(f"Browse logs at: http://localhost:5601")
    print(f"  → Go to: Management → Index Patterns → create pattern '{args.index}*'")


if __name__ == "__main__":
    main()
