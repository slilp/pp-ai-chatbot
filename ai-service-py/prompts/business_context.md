# Payment Platform — Business Context

## Namespaces & Services

| Namespace | Key Containers |
|-----------|---------------|
| `billpay-orchestration` | `ais-payment-submit`, `ais-payment-confirm` |
| `billpay-adapter` | `ais-prepaid-adapter`, `ais-postpaid-adapter`, `true-prepaid-adapter`, `dtac-prepaid-adapter`, `mwa-adapter`, `ktc-payment-adapter`, `cbs-standardpayment-adapter` |
| `common-payment-adapter` | `ekyc-adapter`, `cbs-inquiry-adapter`, `mp-voucher-inquiry-adapter`, `mp-point-adapter`, `ais-nbo-inquiry-adapter` |
| `billpay-processor` | `banking-agent-inquiry-processor`, `billpayment-status-inquiry-processor`, `common-payment-submit`, `common-payment-confirm`, `common-payment-status-inquiry` |
| `biller-profile-module` | `biller-inquiry-processor` |
| `gateway` | `proxy` |
| `partner-payment` | `partner-payment-inquiry`, `partner-payment-request`, `partner-payment-deeplink-validate` |
| `sof-module`, `sof-module-orchestrator` | `sof-reversal`, `casa-payment-processor` |

## Log Document Structure (OpenSearch `k8s-logs` index)

Each document has these TOP-LEVEL fields:
```
@timestamp  — UTC datetime (e.g. "2026-06-08T07:26:32.705Z")
namespace   — Kubernetes namespace (keyword)
pod         — Pod name (keyword)
container   — Container/service name (keyword)
message     — Raw JSON string containing the structured log
log_file    — Source log file path (keyword)
```

**IMPORTANT**: The `message` field is a JSON STRING. All log fields (level, traceId, etc.)
are INSIDE this string. Use `match_phrase` to search for values within it.

### Fields inside `message` JSON
```json
{
  "level": "info|error",
  "type": "Request|ClientRequest|ClientResponse|Response",
  "traceId": "c1656ee32cc78eadbf7554092bb9a19d",
  "spanId": "29e6f7f0e0f45fe6",
  "xRequestId": "7303e5ab-5eea-4045-bbac-be37e6ff5376",
  "xCorrelationId": "887dbb06-...-crid",
  "url": "/api/payment-platform/v3/bpp-module/...",
  "reqMethod": "POST",
  "statusCode": 500,
  "timeInMS": 139,
  "errorCode": "BP50002",
  "respErrorCode": "BP50002",
  "msg": "BP50002 : AIS - Unable to process transaction",
  "time": "2026-06-08 14:26:32.705+07:00"
}
```

**Note on timestamps**: `@timestamp` is stored in UTC. Bangkok time (+07:00) = UTC+7.
When user says "14:26 Bangkok time", query as "07:26 UTC".

## Transaction Flow

A typical AIS payment creates these log entries in order, all sharing the same `traceId`:
1. `ais-payment-submit` | type=`Request` (incoming from client)
2. `ais-payment-submit` | type=`ClientRequest` → `banking-agent-inquiry-processor`
3. `banking-agent-inquiry-processor` | type=`Request`/`Response`
4. `ais-payment-submit` | type=`ClientRequest` → `biller-inquiry-processor`
5. `ais-payment-submit` | type=`ClientRequest` → `ekyc-adapter`
6. `ekyc-adapter` | type=`Request`/`ClientRequest`/`ClientResponse`/`Response`
7. `ais-payment-submit` | type=`ClientRequest` → `ais-prepaid-adapter`
8. `ais-prepaid-adapter` | type=`Request`/`ClientRequest` → AIS ESAD SOAP
9. `ais-prepaid-adapter` | type=`ClientResponse` (SOAP response with Status/Message)
10. `ais-payment-submit` | type=`ClientResponse` (adapter result)
11. `ais-payment-submit` | level=`error` (if failed, with `respErrorCode`)
12. `ais-payment-submit` | type=`Response` (final response to client)

## Error Code System

### Internal BP Codes (our system)
Appear in `ais-payment-submit` (and other services) as `respErrorCode` or `errorCode` in the `message` JSON.

### External Codes (from adapter logs)
Come from external services in `ais-prepaid-adapter`, `ais-postpaid-adapter`, etc.
Found in SOAP `<Status>` and `<Message>` fields inside the adapter's ClientResponse log.

### Known Error Mapping
| BP Code | Description | External Service | External Code | Meaning |
|---------|-------------|-----------------|---------------|---------|
| BP50002 | AIS - Unable to process transaction | AIS ESAD | ITWS006 | Validate Mobile Error — mobile number invalid/not found |
| BP50006 | AIS - Invalid reference | AIS ESAD | (varies) | Invalid ref1/ref2 value |
| BP50013 | AIS - Package was already added or cannot be added | AIS ESAD/NBO | (varies) | Package already purchased |
| BP50299 | Timeout from AIS biller | AIS ESAD | (timeout) | No response within timeout |
| BP53602 | EKYC API fail | EKYC service | (varies) | Customer sanction check failed |
| BP30029 | Transaction Not Found | Internal DB | — | No transaction record found |
| BP52106 | TRUE - Invalid reference | TRUE DTAC | (varies) | Invalid reference number |

### Root Cause Analysis
When a transaction fails with a BP code:
1. Find the BP error in the submit service (`respErrorCode` in message)
2. Use the same `traceId` to find the adapter's ClientResponse log
3. In the adapter's ClientResponse, look for the SOAP `<Status>` and `<Message>` fields
4. Explain BOTH: our BP code + the external root cause
