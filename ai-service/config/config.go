package config

import "os"

type Config struct {
	Provider      string // "lmstudio" or "claude"
	BaseURL       string
	APIKey        string
	Model         string
	OpenSearchURL string
	Port          string
}

func Load() Config {
	return Config{
		Provider:      getEnv("LLM_PROVIDER", "lmstudio"),
		BaseURL:       getEnv("LLM_BASE_URL", "http://localhost:1234/v1"),
		APIKey:        getEnv("LLM_API_KEY", "lm-studio"),
		Model:         getEnv("LLM_MODEL", "qwen/qwen3-14b"),
		OpenSearchURL: getEnv("OPENSEARCH_URL", "http://localhost:9200"),
		Port:          getEnv("PORT", "8080"),
	}
}

func getEnv(key, fallback string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return fallback
}
