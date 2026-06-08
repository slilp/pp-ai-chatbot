package main

import (
	"log"
	"os"

	"ai-service/config"
	"ai-service/handler"
	"ai-service/llm"
	"ai-service/store"

	"github.com/gin-gonic/gin"
)

func main() {
	cfg := config.Load()

	var client llm.LLMClient
	switch cfg.Provider {
	case "lmstudio":
		client = llm.NewLMStudioClient(cfg.BaseURL, cfg.APIKey, cfg.Model)
	default:
		log.Fatalf("unknown LLM_PROVIDER: %s", cfg.Provider)
	}

	skillMD, err := os.ReadFile("skill.md")
	if err != nil {
		log.Fatalf("read skill.md: %v", err)
	}

	mockStore, err := store.NewMockStore("mock.json")
	if err != nil {
		log.Fatalf("load mock.json: %v", err)
	}

	chatHandler := handler.NewChatHandler(client, string(skillMD), mockStore)

	r := gin.Default()
	r.POST("/chat", chatHandler.Handle)

	log.Printf("starting server on :%s (provider=%s model=%s)", cfg.Port, cfg.Provider, cfg.Model)
	if err := r.Run(":" + cfg.Port); err != nil {
		log.Fatalf("server error: %v", err)
	}
}
