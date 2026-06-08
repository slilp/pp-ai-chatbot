#!/usr/bin/env bash
# Dump logs from all pods in a Kubernetes cluster to ./logs/<namespace>/<pod>/<container>.log
#
# Examples:
#   ./dump-k8s-logs.sh
#   ./dump-k8s-logs.sh --namespace payment --since 24h
#   ./dump-k8s-logs.sh --context my-cluster --since 7d

set -euo pipefail

NAMESPACE=""
SINCE="72h"
CONTEXT=""
OUTPUT_DIR="./logs"
EXCLUDE_RAW="amazon-cloudwatch,argocd,devops"

usage() {
  echo "Usage: $0 [--namespace <ns>] [--since <duration>] [--context <kube-context>] [--output <dir>] [--exclude <ns,...>]"
  echo ""
  echo "  --namespace  Only dump logs from this namespace (default: all namespaces)"
  echo "  --since      Fetch logs since this duration, e.g. 24h, 7d (default: 72h)"
  echo "  --context    Kubernetes context to use (default: current context)"
  echo "  --output     Local directory to write logs into (default: ./logs)"
  echo "  --exclude    Comma-separated namespaces to skip (default: amazon-cloudwatch,argocd,devops)"
  exit 0
}

while [[ $# -gt 0 ]]; do
  case $1 in
    --namespace) NAMESPACE="$2"; shift 2 ;;
    --since)     SINCE="$2";     shift 2 ;;
    --context)   CONTEXT="$2";   shift 2 ;;
    --output)    OUTPUT_DIR="$2"; shift 2 ;;
    --exclude)   EXCLUDE_RAW="$2"; shift 2 ;;
    -h|--help)   usage ;;
    *) echo "Unknown option: $1"; usage ;;
  esac
done

IFS=',' read -ra EXCLUDE_NAMESPACES <<< "$EXCLUDE_RAW"

is_excluded() {
  local ns="$1"
  local ex
  for ex in "${EXCLUDE_NAMESPACES[@]}"; do
    if [[ "$ns" == "$ex" ]]; then
      return 0
    fi
  done
  return 1
}

# Populate global LINES_OUT array from command output, silently skipping failures.
read_lines() {
  LINES_OUT=()
  mapfile -t LINES_OUT < <("$@" 2>/dev/null || true)
}

KUBECTL="kubectl"
if [[ -n "$CONTEXT" ]]; then
  KUBECTL="kubectl --context=$CONTEXT"
fi

# Collect namespaces
if [[ -n "$NAMESPACE" ]]; then
  NAMESPACES=("$NAMESPACE")
else
  read_lines $KUBECTL get namespaces \
    -o jsonpath='{range .items[*]}{.metadata.name}{"\n"}{end}'
  NAMESPACES=("${LINES_OUT[@]}")
fi

echo "Excluding namespaces : ${EXCLUDE_NAMESPACES[*]}"
echo "Since                : $SINCE"
echo "Output directory     : $OUTPUT_DIR"
echo ""

TOTAL_FILES=0
FAILED=0

for NS in "${NAMESPACES[@]}"; do
  if [[ -z "$NS" ]]; then continue; fi

  if is_excluded "$NS"; then
    echo "[skip] $NS"
    continue
  fi

  read_lines $KUBECTL get pods -n "$NS" \
    -o jsonpath='{range .items[*]}{.metadata.name}{"\n"}{end}'

  if [[ ${#LINES_OUT[@]} -eq 0 ]]; then continue; fi

  for POD in "${LINES_OUT[@]}"; do
    if [[ -z "$POD" ]]; then continue; fi

    read_lines $KUBECTL get pod "$POD" -n "$NS" \
      -o jsonpath='{range .spec.initContainers[*]}{.name}{"\n"}{end}{range .spec.containers[*]}{.name}{"\n"}{end}'

    if [[ ${#LINES_OUT[@]} -eq 0 ]]; then continue; fi

    for CONTAINER in "${LINES_OUT[@]}"; do
      if [[ -z "$CONTAINER" ]]; then continue; fi

      DIR="$OUTPUT_DIR/$NS/$POD"
      mkdir -p "$DIR"
      FILE="$DIR/${CONTAINER}.log"

      printf "  %-60s" "$NS/$POD/$CONTAINER"

      if $KUBECTL logs "$POD" -n "$NS" -c "$CONTAINER" \
          --since="$SINCE" --timestamps=true > "$FILE" 2>/dev/null; then
        LINES=$(wc -l < "$FILE" | tr -d ' ')
        echo "OK ($LINES lines)"
        TOTAL_FILES=$((TOTAL_FILES + 1))
      else
        echo "FAILED (skipped)"
        rm -f "$FILE"
        FAILED=$((FAILED + 1))
      fi
    done
  done
done

echo ""
echo "Done. Wrote $TOTAL_FILES log file(s). Failures: $FAILED."
echo "Logs saved to: $OUTPUT_DIR"
