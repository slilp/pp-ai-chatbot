package llm

import "context"

type Message struct {
	Role    string // "system", "user", "assistant"
	Content string
}

type LLMClient interface {
	Chat(ctx context.Context, messages []Message) (string, error)
	ChatStream(ctx context.Context, messages []Message, onToken func(string) error) error
}
