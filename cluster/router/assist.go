package main

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"strings"
	"time"
)

// Assistant is the optional Phase 5 1 B-class LLM on the Pi that breaks
// classifier ties. Created lazily in main; passed into Classifier via
// SetAssistant so the classifier remains decoupled from HTTP.
type Assistant struct {
	URL       string
	Model     string
	MaxChars  int
	Timeout   time.Duration
	Client    *http.Client
}

// NewAssistant returns nil if cfg.RouterAssist is disabled — callers should
// check for nil rather than always asking the assistant something.
func NewAssistant(cfg RouterAssist) *Assistant {
	if !cfg.Enabled {
		return nil
	}
	timeout := time.Duration(cfg.TimeoutMs) * time.Millisecond
	return &Assistant{
		URL:      strings.TrimRight(cfg.URL, "/"),
		Model:    cfg.Model,
		MaxChars: cfg.MaxChars,
		Timeout:  timeout,
		Client:   &http.Client{Timeout: timeout},
	}
}

// AssistDecision is what the assistant returns. Tier is empty string on
// failure (caller falls back to the static answer).
type AssistDecision struct {
	Tier       string        // "soft" | "hard" | "" (failure)
	Latency    time.Duration // round-trip time
	Raw        string        // first ~50 chars of the model's reply (debug)
	Err        error         // non-nil if Tier == ""
}

// Decide asks the assist model whether this user prompt is more like a
// "small/fast" job (→ soft) or "large/capable" (→ hard). The reply is a
// single character: "1" or "2".
//
// Implementation: POST /api/generate (Ollama native shape, stream=false),
// temperature 0 for determinism. Robust to extra whitespace and the model
// echoing the prompt — we look for the first "1" or "2" in the response.
func (a *Assistant) Decide(ctx context.Context, userMsg string) AssistDecision {
	start := time.Now()
	dec := AssistDecision{}

	prompt := buildAssistPrompt(userMsg, a.MaxChars)
	body, err := json.Marshal(map[string]any{
		"model":  a.Model,
		"prompt": prompt,
		"stream": false,
		"options": map[string]any{
			"temperature": 0.0,
			"num_predict": 8, // tiny — we only need one digit
		},
	})
	if err != nil {
		dec.Err = err
		return dec
	}

	ctx, cancel := context.WithTimeout(ctx, a.Timeout)
	defer cancel()
	req, err := http.NewRequestWithContext(ctx, "POST", a.URL+"/api/generate", bytes.NewReader(body))
	if err != nil {
		dec.Err = err
		return dec
	}
	req.Header.Set("Content-Type", "application/json")

	resp, err := a.Client.Do(req)
	if err != nil {
		dec.Err = err
		return dec
	}
	defer resp.Body.Close()
	if resp.StatusCode >= 400 {
		dec.Err = errors.New("assist HTTP " + resp.Status)
		return dec
	}

	respBytes, err := io.ReadAll(resp.Body)
	if err != nil {
		dec.Err = err
		return dec
	}
	var payload struct {
		Response string `json:"response"`
	}
	if err := json.Unmarshal(respBytes, &payload); err != nil {
		dec.Err = err
		return dec
	}

	raw := payload.Response
	if len(raw) > 50 {
		dec.Raw = raw[:50]
	} else {
		dec.Raw = raw
	}
	dec.Tier = parseAssistResponse(raw)
	if dec.Tier == "" {
		dec.Err = errors.New("could not parse '1' or '2' from response: " + dec.Raw)
	}
	dec.Latency = time.Since(start)
	return dec
}

// buildAssistPrompt — short, deterministic system + user template. Truncates
// the user message at maxChars so a runaway prompt can't make the assist
// model burn 30 s on a Pi.
func buildAssistPrompt(userMsg string, maxChars int) string {
	if maxChars > 0 && len(userMsg) > maxChars {
		userMsg = userMsg[:maxChars] + "...[truncated]"
	}
	return `You are a routing classifier. Decide whether the user's coding task is better served by:
1 = a small/fast model (one-line fixes, typos, formatting, simple lookups, single-function edits)
2 = a large/capable model (multi-file refactors, architecture decisions, complex debugging, long-context analysis)

Reply with ONLY the single digit "1" or "2". No explanation, no other text.

User task:
` + userMsg + `

Answer (1 or 2):`
}

// parseAssistResponse — scan for the first "1" or "2" character. Tolerates
// the model adding whitespace, periods, or a brief preamble despite the
// "no other text" instruction.
func parseAssistResponse(s string) string {
	for _, r := range s {
		switch r {
		case '1':
			return "soft"
		case '2':
			return "hard"
		}
	}
	return ""
}
