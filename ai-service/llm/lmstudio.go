package llm

import (
	"context"
	"errors"
	"io"
	"strings"

	openai "github.com/sashabaranov/go-openai"
)

type LMStudioClient struct {
	client *openai.Client
	model  string
}

func NewLMStudioClient(baseURL, apiKey, model string) *LMStudioClient {
	cfg := openai.DefaultConfig(apiKey)
	cfg.BaseURL = baseURL
	return &LMStudioClient{
		client: openai.NewClientWithConfig(cfg),
		model:  model,
	}
}

func (c *LMStudioClient) Chat(ctx context.Context, messages []Message) (string, error) {
	resp, err := c.client.CreateChatCompletion(ctx, openai.ChatCompletionRequest{
		Model:     c.model,
		Messages:  toOpenAI(messages),
		MaxTokens: 2048,
	})
	if err != nil {
		return "", err
	}
	return resp.Choices[0].Message.Content, nil
}

func (c *LMStudioClient) ChatStream(ctx context.Context, messages []Message, onToken func(string) error) error {
	stream, err := c.client.CreateChatCompletionStream(ctx, openai.ChatCompletionRequest{
		Model:     c.model,
		Messages:  toOpenAI(messages),
		MaxTokens: 2048,
		Stream:    true,
	})
	if err != nil {
		return err
	}
	defer stream.Close()

	for {
		resp, err := stream.Recv()
		if errors.Is(err, io.EOF) {
			return nil
		}
		if err != nil {
			// LM Studio closes the stream early when context is full — treat as done
			if strings.Contains(err.Error(), "unexpected end of JSON input") ||
				strings.Contains(err.Error(), "EOF") {
				return nil
			}
			return err
		}
		if len(resp.Choices) > 0 {
			token := resp.Choices[0].Delta.Content
			if token != "" {
				if err := onToken(token); err != nil {
					return err
				}
			}
		}
	}
}

func toOpenAI(messages []Message) []openai.ChatCompletionMessage {
	out := make([]openai.ChatCompletionMessage, len(messages))
	for i, m := range messages {
		out[i] = openai.ChatCompletionMessage{Role: m.Role, Content: m.Content}
	}
	return out
}
