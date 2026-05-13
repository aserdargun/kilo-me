package main

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"
)

func mockAssistServer(t *testing.T, response string, delay time.Duration, status int) *httptest.Server {
	return httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/api/generate" {
			t.Errorf("unexpected path: %s", r.URL.Path)
		}
		if delay > 0 {
			time.Sleep(delay)
		}
		if status != 0 && status != http.StatusOK {
			w.WriteHeader(status)
			return
		}
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(map[string]any{"response": response})
	}))
}

func newAssistant(url string, timeoutMs int) *Assistant {
	return NewAssistant(RouterAssist{
		Enabled:   true,
		URL:       url,
		Model:     "qwen2.5:1.5b",
		TimeoutMs: timeoutMs,
		MaxChars:  2000,
	})
}

func TestNewAssistant_DisabledReturnsNil(t *testing.T) {
	if a := NewAssistant(RouterAssist{Enabled: false}); a != nil {
		t.Errorf("expected nil when disabled, got %+v", a)
	}
}

func TestAssistant_Decide_VotesSoft(t *testing.T) {
	srv := mockAssistServer(t, "1", 0, 0)
	defer srv.Close()
	a := newAssistant(srv.URL, 1500)
	dec := a.Decide(testCtx(t), "fix this typo in README.md")
	if dec.Err != nil {
		t.Fatalf("unexpected error: %v", dec.Err)
	}
	if dec.Tier != "soft" {
		t.Errorf("want soft, got %q (raw=%s)", dec.Tier, dec.Raw)
	}
}

func TestAssistant_Decide_VotesHard(t *testing.T) {
	srv := mockAssistServer(t, "2", 0, 0)
	defer srv.Close()
	a := newAssistant(srv.URL, 1500)
	dec := a.Decide(testCtx(t), "refactor the auth subsystem across 12 files")
	if dec.Err != nil {
		t.Fatalf("unexpected error: %v", dec.Err)
	}
	if dec.Tier != "hard" {
		t.Errorf("want hard, got %q", dec.Tier)
	}
}

func TestAssistant_Decide_ToleratesWhitespaceAndPunctuation(t *testing.T) {
	// Real 1B models often disobey "no other text" — must handle "\n  2." etc.
	srv := mockAssistServer(t, "\n  2.\n", 0, 0)
	defer srv.Close()
	a := newAssistant(srv.URL, 1500)
	dec := a.Decide(testCtx(t), "any prompt")
	if dec.Tier != "hard" {
		t.Errorf("noisy '2' parse: want hard, got %q", dec.Tier)
	}
}

func TestAssistant_Decide_TimeoutFallsBack(t *testing.T) {
	srv := mockAssistServer(t, "1", 200*time.Millisecond, 0)
	defer srv.Close()
	// 50ms timeout < 200ms server delay → should fail.
	a := newAssistant(srv.URL, 50)
	dec := a.Decide(testCtx(t), "any prompt")
	if dec.Tier != "" {
		t.Errorf("expected empty tier on timeout, got %q", dec.Tier)
	}
	if dec.Err == nil {
		t.Errorf("expected non-nil err on timeout")
	}
}

func TestAssistant_Decide_HTTPErrorFallsBack(t *testing.T) {
	srv := mockAssistServer(t, "1", 0, http.StatusInternalServerError)
	defer srv.Close()
	a := newAssistant(srv.URL, 1500)
	dec := a.Decide(testCtx(t), "any prompt")
	if dec.Tier != "" {
		t.Errorf("expected empty tier on 500, got %q", dec.Tier)
	}
	if dec.Err == nil {
		t.Errorf("expected non-nil err on 500")
	}
}

func TestAssistant_Decide_UnparseableFallsBack(t *testing.T) {
	srv := mockAssistServer(t, "I think probably the second option", 0, 0)
	defer srv.Close()
	a := newAssistant(srv.URL, 1500)
	dec := a.Decide(testCtx(t), "any prompt")
	// "2" appears nowhere — parser returns "".
	// Note: the word "second" contains no '1' or '2' digit chars.
	if dec.Tier != "" {
		t.Errorf("expected empty tier on unparseable response, got %q (raw=%s)", dec.Tier, dec.Raw)
	}
}

func TestAssistant_Decide_TruncatesLongPrompts(t *testing.T) {
	// Server echoes the received prompt back so we can inspect it.
	gotPrompt := ""
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		var body map[string]any
		_ = json.NewDecoder(r.Body).Decode(&body)
		if p, ok := body["prompt"].(string); ok {
			gotPrompt = p
		}
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(map[string]any{"response": "1"})
	}))
	defer srv.Close()

	a := newAssistant(srv.URL, 1500)
	long := make([]byte, 5000)
	for i := range long {
		long[i] = 'x'
	}
	_ = a.Decide(testCtx(t), string(long))
	if len(gotPrompt) > 3000 {
		t.Errorf("prompt not truncated to ~maxChars; got %d chars", len(gotPrompt))
	}
}

// TestClassifier_AssistOverridesAmbiguous — integration test. The model is
// hosted on both tiers and the request is small (no token / tool escalation),
// so we'd normally land on the alphabetical "hard". With the assistant voting
// "1" (soft), we should pick soft instead.
func TestClassifier_AssistOverridesAmbiguous(t *testing.T) {
	srv := mockAssistServer(t, "1", 0, 0)
	defer srv.Close()

	cfg := newTestConfig()
	cfg.RouterAssist = RouterAssist{
		Enabled:   true,
		URL:       srv.URL,
		Model:     "qwen2.5:1.5b",
		TimeoutMs: 1500,
		MaxChars:  2000,
	}

	c := NewClassifier(cfg)
	c.SetAssistant(NewAssistant(cfg.RouterAssist))

	tier, reason := c.ClassifyWithReason(&ChatRequest{
		Model: "phi-4:14b-q4_K_M", // on both tiers
		Messages: []ChatMessage{
			{Role: "user", Content: "fix this typo"},
		},
	})
	if tier != "soft" {
		t.Errorf("assist=soft should win: got tier=%q reason=%q", tier, reason)
	}
	if reason[:6] != "assist" {
		t.Errorf("expected assist reason, got %q", reason)
	}
}

func TestClassifier_AssistFailureFallsBackToStatic(t *testing.T) {
	srv := mockAssistServer(t, "", 0, http.StatusInternalServerError)
	defer srv.Close()

	cfg := newTestConfig()
	cfg.RouterAssist = RouterAssist{
		Enabled:   true,
		URL:       srv.URL,
		Model:     "qwen2.5:1.5b",
		TimeoutMs: 100,
		MaxChars:  2000,
	}
	c := NewClassifier(cfg)
	c.SetAssistant(NewAssistant(cfg.RouterAssist))

	tier, reason := c.ClassifyWithReason(&ChatRequest{
		Model:    "phi-4:14b-q4_K_M",
		Messages: []ChatMessage{{Role: "user", Content: "fix this typo"}},
	})
	if tier != "hard" {
		t.Errorf("static fallback should win on assist failure: got %q", tier)
	}
	if reason == "" || reason[:6] == "assist" {
		t.Errorf("expected non-assist reason on failure, got %q", reason)
	}
}

// testCtx — context.Background() shorthand that compiles without importing
// context in every test (kept here for readability).
func testCtx(t *testing.T) context.Context {
	t.Helper()
	return context.Background()
}
