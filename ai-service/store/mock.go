package store

import (
	"encoding/json"
	"os"
	"strings"
	"time"
)

type LogEntry struct {
	ContainerName string         `json:"container-name"`
	Timestamp     string         `json:"timestamp"`
	Log           map[string]any `json:"log"`
}

type Intent struct {
	TraceId       string `json:"traceId"`
	TransactionId string `json:"transactionId"`
	ContainerName string `json:"containerName"`
	ErrorCode     string `json:"errorCode"`
	Keyword       string `json:"keyword"`
	TimeFrom      string `json:"timeFrom"` // RFC3339 or "HH:MM"
	TimeTo        string `json:"timeTo"`   // RFC3339 or "HH:MM"
}

type MockStore struct {
	entries []LogEntry
}

func NewMockStore(path string) (*MockStore, error) {
	b, err := os.ReadFile(path)
	if err != nil {
		return nil, err
	}
	var entries []LogEntry
	if err := json.Unmarshal(b, &entries); err != nil {
		return nil, err
	}
	return &MockStore{entries: entries}, nil
}

func (s *MockStore) Query(intent Intent) []LogEntry {
	from, to := parseTimeRange(intent.TimeFrom, intent.TimeTo)

	var result []LogEntry
	for _, e := range s.entries {
		if !matchesIntent(e, intent) {
			continue
		}
		if !matchesTimeRange(e.Timestamp, from, to) {
			continue
		}
		result = append(result, e)
	}
	return result
}

func matchesIntent(e LogEntry, intent Intent) bool {
	logStr := logAsString(e)

	if intent.TraceId != "" && !strings.Contains(logStr, intent.TraceId) {
		return false
	}
	if intent.TransactionId != "" && !strings.Contains(logStr, intent.TransactionId) {
		return false
	}
	if intent.ContainerName != "" && !strings.EqualFold(e.ContainerName, intent.ContainerName) {
		return false
	}
	if intent.ErrorCode != "" && !strings.Contains(logStr, intent.ErrorCode) {
		return false
	}
	if intent.Keyword != "" && !strings.Contains(strings.ToLower(logStr), strings.ToLower(intent.Keyword)) {
		return false
	}
	return true
}

func matchesTimeRange(ts string, from, to *time.Time) bool {
	if from == nil && to == nil {
		return true
	}
	if ts == "" {
		return true
	}
	// try RFC3339Nano first (has milliseconds), then plain RFC3339
	t, err := time.Parse(time.RFC3339Nano, ts)
	if err != nil {
		t, err = time.Parse(time.RFC3339, ts)
	}
	if err != nil {
		return true
	}
	if from != nil && t.Before(*from) {
		return false
	}
	if to != nil && t.After(*to) {
		return false
	}
	return true
}

// parseTimeRange accepts RFC3339, "YYYY-MM-DD HH:MM", or "HH:MM".
func parseTimeRange(fromStr, toStr string) (*time.Time, *time.Time) {
	loc, _ := time.LoadLocation("Asia/Bangkok")
	parse := func(s string) *time.Time {
		if s == "" {
			return nil
		}
		// RFC3339
		if t, err := time.Parse(time.RFC3339, s); err == nil {
			return &t
		}
		// "YYYY-MM-DDTHH:MM:SS" without timezone
		if t, err := time.ParseInLocation("2006-01-02T15:04:05", s, loc); err == nil {
			return &t
		}
		// "YYYY-MM-DD HH:MM"
		if t, err := time.ParseInLocation("2006-01-02 15:04", s, loc); err == nil {
			return &t
		}
		// "HH:MM" — use today's date
		if len(s) == 5 && s[2] == ':' {
			now := time.Now().In(loc)
			t := time.Date(now.Year(), now.Month(), now.Day(),
				int(s[0]-'0')*10+int(s[1]-'0'),
				int(s[3]-'0')*10+int(s[4]-'0'),
				0, 0, loc)
			return &t
		}
		return nil
	}
	return parse(fromStr), parse(toStr)
}

func logAsString(e LogEntry) string {
	b, _ := json.Marshal(e)
	return string(b)
}
