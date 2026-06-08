# Payment Platform — Business Spec

## Log Fields
- `container-name`: service that emitted the log
- `timestamp`: ISO 8601 datetime with timezone (e.g. `2026-06-07T20:33:18.333+07:00`) — use this for time-range filtering
- `traceId`: end-to-end trace ID across all services
- `spanId`: span within the trace
- `xRequestId`: request ID per service hop
- `xCorrelationId`: correlation ID from the caller
- `type`: log type — `Request`, `ClientRequest`, `ClientResponse`
- `url`: endpoint called
- `statusCode`: HTTP status code (ClientResponse only)
- `timeInMS`: response time in milliseconds (ClientResponse only)

## Time-based Queries
When a user asks about a time range (e.g. "between 10:00 and 11:00", "on 2026-06-07 from 20:00"):
- Extract `timeFrom` and `timeTo` from the question
- Use format `YYYY-MM-DDTHH:MM:00+07:00` when a date is specified (timezone is Asia/Bangkok = +07:00)
- Use format `HH:MM` when only time is specified (date will be assumed as today)
- The `timestamp` field on each log entry is used for filtering

## Error Code System

There are two layers of error codes:

### Internal BP Codes (our system)
These appear in logs from `ais-payment-submit` as `errorCode` or `respErrorCode`.
They are our system's standardized error codes, prefixed with `BP`.

### External Codes (from adapter ClientResponse logs)
These come from external services via `container-name: *-adapter` logs.
They appear in `Client Response` log entries from the adapter containers.

### Error Mapping Table
When you see a BP error code, look for the corresponding external code in the
adapter's `Client Response` log (same `traceId`) to explain the root cause.

| BP Code | Description | External Service | External Code | External Meaning |
|---------|-------------|-----------------|---------------|-----------------|
| BP50002 | AIS - Unable to process transaction | AIS ESAD (http://ESAD.ais.co.th/) | ITWS006 | Validate Mobile Error — the mobile number is invalid or not found in AIS system |

### How to explain errors to users
When the user asks why a transaction failed:
1. Find the BP error code in the `ais-payment-submit` logs
2. Find the `Client Response` log from the adapter (same `traceId`) to get the external code
3. Explain BOTH: what our system returned (BP code) AND what the external service actually said (external code + meaning)

Example: "Transaction failed with BP50002 (AIS Unable to process). The root cause from AIS ESAD was ITWS006 — Validate Mobile Error, meaning the mobile number was not recognized by AIS."
