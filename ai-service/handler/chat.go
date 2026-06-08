package handler

import (
	"context"
	"encoding/json"
	"fmt"
	"log"
	"net/http"
	"regexp"
	"strings"

	"ai-service/llm"
	"ai-service/store"

	"github.com/gin-gonic/gin"
)

type ChatHandler struct {
	llmClient llm.LLMClient
	skillMD   string
	mockStore *store.MockStore
}

func NewChatHandler(client llm.LLMClient, skillMD string, mockStore *store.MockStore) *ChatHandler {
	return &ChatHandler{llmClient: client, skillMD: skillMD, mockStore: mockStore}
}

type HistoryMessage struct {
	Role    string `json:"role"`
	Content string `json:"content"`
}

type ChatRequest struct {
	Question string           `json:"question" binding:"required"`
	History  []HistoryMessage `json:"history"`
}

func (h *ChatHandler) Handle(c *gin.Context) {
	var req ChatRequest
	if err := c.ShouldBindJSON(&req); err != nil {
		c.JSON(http.StatusBadRequest, gin.H{"error": err.Error()})
		return
	}

	log.Printf("[chat] question: %q", req.Question)

	// Open SSE stream immediately so the frontend doesn't wait
	c.Header("Content-Type", "text/event-stream")
	c.Header("Cache-Control", "no-cache")
	c.Header("Connection", "keep-alive")
	c.Header("X-Accel-Buffering", "no")
	c.Header("Access-Control-Allow-Origin", "*")

	ctx := context.Background()

	// Call 1: extract intent (non-streaming)
	log.Printf("[chat] extracting intent...")
	intent, err := h.extractIntent(ctx, req.Question)
	if err != nil {
		log.Printf("[chat] intent error: %v", err)
		fmt.Fprintf(c.Writer, "data: [ERROR] intent: %s\n\n", err.Error())
		c.Writer.Flush()
		return
	}
	log.Printf("[chat] intent: %+v", intent)

	// Query mock store — prioritize error entries, cap total to keep prompt under ~3000 chars
	logs := h.mockStore.Query(intent)
	log.Printf("[chat] logs found: %d", len(logs))
	selected, truncated := selectLogs(logs)
	rawLogs := formatLogsForLLM(selected, truncated)
	log.Printf("[chat] prompt log size: %d chars (selected %d/%d)", len(rawLogs), len(selected), len(logs))

	// Call 2: stream the analysis
	log.Printf("[chat] starting analyst stream...")
	messages := h.buildAnalystMessages(req.Question, string(rawLogs), req.History)

	filter := newThinkFilter(func(token string) error {
		b, _ := json.Marshal(token)
		fmt.Fprintf(c.Writer, "data: %s\n\n", b)
		c.Writer.Flush()
		return nil
	})

	err = h.llmClient.ChatStream(ctx, messages, filter.write)
	if err != nil {
		log.Printf("[chat] stream error: %v", err)
		fmt.Fprintf(c.Writer, "data: [ERROR] %s\n\n", err.Error())
	} else {
		log.Printf("[chat] stream done")
		fmt.Fprintf(c.Writer, "data: [DONE]\n\n")
	}
	c.Writer.Flush()
}

func (h *ChatHandler) extractIntent(ctx context.Context, question string) (store.Intent, error) {
	messages := []llm.Message{
		{
			Role: "system",
			Content: `Extract search intent from the user's question about payment logs.
Return ONLY valid JSON with these fields (omit fields you cannot determine):
{
  "traceId": "",
  "transactionId": "",
  "containerName": "",
  "errorCode": "",
  "keyword": "",
  "timeFrom": "",
  "timeTo": ""
}
Rules for timeFrom/timeTo:
- If the user gives a date AND time (e.g. "2026-06-07 between 10:00 and 11:00"), use RFC3339: "2026-06-07T10:00:00+07:00"
- If only a time is given (e.g. "between 17:00 and 19:00"), use "HH:MM" format: "17:00"
- Timezone is always Asia/Bangkok (+07:00)
No explanation. No markdown. Just the JSON object.`,
		},
		// /no_think must be in the user turn for Qwen3
		{Role: "user", Content: "/no_think\n" + question},
	}

	raw, err := h.llmClient.Chat(ctx, messages)
	if err != nil {
		return store.Intent{}, err
	}

	// strip any <think> blocks before parsing
	raw = stripThink(raw)

	var intent store.Intent
	if err := json.Unmarshal([]byte(extractJSON(raw)), &intent); err != nil {
		intent = store.Intent{Keyword: question}
	}
	return intent, nil
}

const maxHistoryMessages = 10

func (h *ChatHandler) buildAnalystMessages(question, rawLogs string, history []HistoryMessage) []llm.Message {
	msgs := []llm.Message{
		{
			Role: "system",
			Content: fmt.Sprintf(`You are a payment platform log analyst for KTB.
Use the following business context:

%s

When analyzing errors:
- Find the BP error code in ais-payment-submit logs
- Find the external error code in the adapter Client Response log (same traceId)
- Use the Error Mapping Table to explain the root cause from the external service
- Always explain BOTH the BP code and the external error meaning

Always answer in Thai language. Write a clear, concise plain text answer. Do not use JSON format.`, h.skillMD),
		},
	}

	// include prior conversation turns (capped to avoid context overflow)
	start := 0
	if len(history) > maxHistoryMessages {
		start = len(history) - maxHistoryMessages
	}
	for _, h := range history[start:] {
		role := h.Role
		if role != "user" && role != "assistant" {
			continue
		}
		msgs = append(msgs, llm.Message{Role: role, Content: h.Content})
	}

	msgs = append(msgs, llm.Message{
		Role:    "user",
		Content: fmt.Sprintf("/no_think\nคำถาม: %s\n\nLog entries:\n%s", question, maskPII(rawLogs)),
	})
	return msgs
}

// thinkFilter buffers stream tokens and suppresses <think>...</think> blocks
// and any leading whitespace that follows them.
type thinkFilter struct {
	buf         strings.Builder
	inThink     bool
	seenContent bool // true once the first non-whitespace token has been emitted
	emit        func(string) error
}

func newThinkFilter(emit func(string) error) *thinkFilter {
	return &thinkFilter{emit: emit}
}

func (f *thinkFilter) write(token string) error {
	f.buf.WriteString(token)
	return f.flush()
}

func (f *thinkFilter) flush() error {
	for {
		s := f.buf.String()
		if f.inThink {
			end := strings.Index(s, "</think>")
			if end == -1 {
				// still inside think block — hold everything
				return nil
			}
			// skip the think block including closing tag and trailing newlines
			f.buf.Reset()
			f.buf.WriteString(strings.TrimLeft(s[end+len("</think>"):], "\n\r "))
			f.inThink = false
			continue
		}
		// not in think block
		start := strings.Index(s, "<think>")
		if start == -1 {
			if s == "" {
				return nil
			}
			// suppress leading whitespace until first real content is seen
			if !f.seenContent {
				s = strings.TrimLeft(s, "\n\r ")
				if s == "" {
					f.buf.Reset()
					return nil
				}
				f.seenContent = true
			}
			if err := f.emit(s); err != nil {
				return err
			}
			f.buf.Reset()
			return nil
		}
		// emit content before <think>
		if start > 0 {
			f.seenContent = true
			if err := f.emit(s[:start]); err != nil {
				return err
			}
		}
		f.buf.Reset()
		f.buf.WriteString(s[start+len("<think>"):])
		f.inThink = true
	}
}

func stripThink(s string) string {
	re := regexp.MustCompile(`(?s)<think>.*?</think>`)
	return strings.TrimSpace(re.ReplaceAllString(s, ""))
}

func maskPII(s string) string {
	patterns := []struct {
		re   *regexp.Regexp
		mask string
	}{
		{regexp.MustCompile(`"customerName"\s*:\s*"[^"]+"`), `"customerName":"***"`},
		{regexp.MustCompile(`"MobileNo"\s*:\s*"[^"]+"`), `"MobileNo":"***"`},
		{regexp.MustCompile(`"cid"\s*:\s*"[^"]+"`), `"cid":"***"`},
		{regexp.MustCompile(`"sofAccount"\s*:\s*"[^"]+"`), `"sofAccount":"***"`},
		{regexp.MustCompile(`"sofAccountName"\s*:\s*"[^"]+"`), `"sofAccountName":"***"`},
		{regexp.MustCompile(`"ref1"\s*:\s*"[^"]+"`), `"ref1":"***"`},
	}
	for _, p := range patterns {
		s = p.re.ReplaceAllString(s, p.mask)
	}
	return s
}

// maxLogEntries controls how many log entries are sent to the LLM.
// Raise this if your LM Studio context window is larger (8K → 50, 16K → 100+).
const maxLogEntries = 20

// selectLogs keeps complete traces that contain at least one error entry.
// If no errors exist, falls back to all entries. Caps at maxLogEntries total.
// Returns the selected slice and how many entries were dropped.
func selectLogs(logs []store.LogEntry) ([]store.LogEntry, int) {
	const maxEntries = maxLogEntries

	// group entries by traceId, preserving order within each trace
	type trace struct {
		entries  []store.LogEntry
		hasError bool
	}
	traceOrder := []string{}
	traces := map[string]*trace{}
	for _, e := range logs {
		tid := fmt.Sprintf("%v", e.Log["traceId"])
		if tid == "" {
			tid = "__no_trace__"
		}
		if _, ok := traces[tid]; !ok {
			traces[tid] = &trace{}
			traceOrder = append(traceOrder, tid)
		}
		t := traces[tid]
		t.entries = append(t.entries, e)
		if fmt.Sprintf("%v", e.Log["level"]) == "error" {
			t.hasError = true
		}
	}

	// prefer traces with errors; fall back to all traces if none have errors
	var selected []store.LogEntry
	for _, pass := range []bool{true, false} {
		for _, tid := range traceOrder {
			t := traces[tid]
			if pass && !t.hasError {
				continue
			}
			if !pass && t.hasError {
				continue // already added
			}
			selected = append(selected, t.entries...)
			if len(selected) >= maxEntries {
				break
			}
		}
		if len(selected) > 0 {
			break
		}
	}

	if len(selected) > maxEntries {
		selected = trimTrace(selected, maxEntries)
		return selected, len(logs) - len(selected)
	}
	return selected, len(logs) - len(selected)
}

// trimTrace reduces a single trace to maxN entries while always keeping error entries.
// Fills remaining slots with the first and last info entries for context.
func trimTrace(entries []store.LogEntry, maxN int) []store.LogEntry {
	var errors, others []store.LogEntry
	for _, e := range entries {
		if fmt.Sprintf("%v", e.Log["level"]) == "error" {
			errors = append(errors, e)
		} else {
			others = append(others, e)
		}
	}
	// always keep all error entries (up to maxN)
	if len(errors) >= maxN {
		return errors[:maxN]
	}
	slots := maxN - len(errors)
	// fill with first half + last half of info entries for request→response context
	var context []store.LogEntry
	if len(others) <= slots {
		context = others
	} else {
		half := slots / 2
		context = append(others[:half], others[len(others)-( slots-half):]...)
	}
	return append(context, errors...)
}

// formatLogsForLLM produces a compact plain-text summary of log entries.
func formatLogsForLLM(logs []store.LogEntry, dropped int) string {
	if len(logs) == 0 {
		return "(no logs found)"
	}
	var sb strings.Builder
	if dropped > 0 {
		fmt.Fprintf(&sb, "(note: %d additional entries omitted — showing errors and first info entries)\n\n", dropped)
	}
	for i, e := range logs {
		fmt.Fprintf(&sb, "--- Log %d ---\n", i+1)
		fmt.Fprintf(&sb, "container: %s\n", e.ContainerName)
		fmt.Fprintf(&sb, "timestamp: %s\n", e.Timestamp)

		get := func(key string) string {
			v, ok := e.Log[key]
			if !ok {
				return ""
			}
			return fmt.Sprintf("%v", v)
		}
		trunc := func(s string, n int) string {
			if len(s) > n {
				return s[:n] + "..."
			}
			return s
		}

		for _, key := range []string{"level", "type", "traceId", "url", "statusCode", "timeInMS"} {
			if val := get(key); val != "" {
				fmt.Fprintf(&sb, "%s: %s\n", key, val)
			}
		}

		// msg: for ClientResponse entries try to extract SOAP Status/Message first
		if msg := get("msg"); msg != "" {
			if status := extractSoapStatus(msg); status != "" {
				fmt.Fprintf(&sb, "soapStatus: %s\n", status)
			} else {
				fmt.Fprintf(&sb, "msg: %s\n", trunc(msg, 300))
			}
		}

		// explicit error fields
		for _, key := range []string{"error", "errorCode", "respErrorCode", "respErrorDesc",
			"resultCode", "resultDesc", "Service Response"} {
			if val := get(key); val != "" {
				fmt.Fprintf(&sb, "%s: %s\n", key, trunc(val, 300))
			}
		}

		sb.WriteString("\n")
	}
	return sb.String()
}

var soapStatusRe = regexp.MustCompile(`<Status>([^<]+)</Status>.*?<Message>([^<]+)</Message>`)

func extractSoapStatus(msg string) string {
	m := soapStatusRe.FindStringSubmatch(msg)
	if m == nil {
		return ""
	}
	return fmt.Sprintf("%s (%s)", m[1], m[2])
}

func extractJSON(s string) string {
	s = strings.TrimSpace(s)
	start := strings.Index(s, "{")
	end := strings.LastIndex(s, "}")
	if start == -1 || end == -1 || end <= start {
		return s
	}
	return s[start : end+1]
}
