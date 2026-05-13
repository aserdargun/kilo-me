package main

import (
	"encoding/json"
	"testing"
)

func newTestConfig() *Config {
	return &Config{
		Workers: map[string]Worker{
			"soft": {
				URL: "http://macmini:11434",
				Models: []string{
					"qwen3-coder:7b-instruct-q5_K_M",
					"llama3.3:8b-instruct-q5_K_M",
					"phi-4:14b-q4_K_M", // also on hard
					"gemma3:4b",
				},
			},
			"hard": {
				URL: "http://rtx:11434",
				Models: []string{
					"qwen3-coder:14b-instruct-q8_0",
					"devstral:22b-q5_K_M",
					"deepseek-r1-distill-qwen:14b-q5_K_M",
					"phi-4:14b-q4_K_M", // also on soft
				},
			},
		},
		Routing: Routing{
			TokenThresholdForHard:     8000,
			ToolCountThresholdForHard: 5,
			DefaultTier:               "soft",
		},
	}
}

func TestClassify_UnambiguousModel(t *testing.T) {
	c := NewClassifier(newTestConfig())
	cases := map[string]string{
		"qwen3-coder:7b-instruct-q5_K_M":     "soft",
		"qwen3-coder:14b-instruct-q8_0":      "hard",
		"devstral:22b-q5_K_M":                "hard",
		"llama3.3:8b-instruct-q5_K_M":        "soft",
		"gemma3:4b":                          "soft",
		"deepseek-r1-distill-qwen:14b-q5_K_M": "hard",
	}
	for model, want := range cases {
		got := c.Classify(&ChatRequest{Model: model})
		if got != want {
			t.Errorf("model=%s: got tier %q, want %q", model, got, want)
		}
	}
}

func TestClassify_AmbiguousModelSmallContext(t *testing.T) {
	// phi-4:14b exists on both. Small request → first-alphabetical = "hard".
	c := NewClassifier(newTestConfig())
	got := c.Classify(&ChatRequest{
		Model: "phi-4:14b-q4_K_M",
		Messages: []ChatMessage{
			{Role: "user", Content: "fix this typo"},
		},
	})
	// Alphabetically, "hard" sorts before "soft".
	if got != "hard" {
		t.Errorf("ambiguous + small: got %q, want hard", got)
	}
}

func TestClassify_AmbiguousModelLargeContext(t *testing.T) {
	// phi-4:14b ambiguous + large prompt → hard via token threshold.
	c := NewClassifier(newTestConfig())
	long := make([]byte, 40000) // ~10000 tokens
	for i := range long {
		long[i] = 'a'
	}
	got := c.Classify(&ChatRequest{
		Model: "phi-4:14b-q4_K_M",
		Messages: []ChatMessage{
			{Role: "user", Content: string(long)},
		},
	})
	if got != "hard" {
		t.Errorf("ambiguous + large context: got %q, want hard", got)
	}
}

func TestClassify_UnknownModel(t *testing.T) {
	c := NewClassifier(newTestConfig())
	got := c.Classify(&ChatRequest{Model: "made-up-model:1b"})
	if got != "soft" {
		t.Errorf("unknown model: got %q, want soft (default tier)", got)
	}
}

func TestClassify_ManyTools(t *testing.T) {
	c := NewClassifier(newTestConfig())
	tools := make([]json.RawMessage, 7)
	for i := range tools {
		tools[i] = json.RawMessage(`{}`)
	}
	got := c.Classify(&ChatRequest{
		Model: "phi-4:14b-q4_K_M",
		Tools: tools,
	})
	if got != "hard" {
		t.Errorf("many tools: got %q, want hard", got)
	}
}
