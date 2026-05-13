package main

import (
	"context"
	"encoding/json"
	"sort"
)

// ChatRequest is the minimum subset of an OpenAI chat-completions body the
// classifier needs. Unknown fields are preserved by the proxy since we
// re-serialize from the raw bytes — this struct is read-only.
type ChatRequest struct {
	Model    string            `json:"model"`
	Messages []ChatMessage     `json:"messages"`
	Tools    []json.RawMessage `json:"tools,omitempty"`
}

type ChatMessage struct {
	Role    string `json:"role"`
	Content string `json:"content"`
}

// Classifier picks a tier for an incoming request using the rules declared
// in Rule 07: first model → tier mapping (config), then token-count and
// tool-count heuristics, then optionally the Phase 5 LLM router-assist,
// then a config default.
type Classifier struct {
	cfg       *Config
	modelTier map[string][]string // model → set of tiers hosting it
	assist    *Assistant          // nil when router_assist.enabled=false
}

func NewClassifier(cfg *Config) *Classifier {
	c := &Classifier{cfg: cfg, modelTier: make(map[string][]string)}
	for tier, w := range cfg.Workers {
		for _, model := range w.Models {
			c.modelTier[model] = append(c.modelTier[model], tier)
		}
	}
	return c
}

// SetAssistant wires the Phase 5 LLM router-assist into the classifier.
// Pass nil to disable. The classifier will silently degrade to static rules
// if the assistant fails or times out.
func (c *Classifier) SetAssistant(a *Assistant) {
	c.assist = a
}

// Classify returns the tier name the request should land on.
//
// Algorithm (first match wins):
//  1. Model hosted on exactly one tier → that tier.
//  2. Estimated token count > threshold → "hard" if declared, else default.
//  3. Tool array length > threshold → "hard" if declared, else default.
//  4. Model hosted on multiple tiers → first tier (alphabetical).
//  5. Otherwise → config.Routing.DefaultTier.
func (c *Classifier) Classify(req *ChatRequest) string {
	tier, _ := c.ClassifyWithReason(req)
	return tier
}

// ClassifyWithReason returns the same tier as Classify plus a short
// human-readable explanation of which rule fired. Used by /v1/classify.
func (c *Classifier) ClassifyWithReason(req *ChatRequest) (tier string, reason string) {
	tiers := c.modelTier[req.Model]

	if len(tiers) == 1 {
		return tiers[0], "model hosted on exactly one tier"
	}

	tokens := estimateTokens(req)
	if tokens > c.cfg.Routing.TokenThresholdForHard {
		if _, ok := c.cfg.Workers["hard"]; ok {
			return "hard", "estimated tokens > token_threshold_for_hard"
		}
	}
	if len(req.Tools) > c.cfg.Routing.ToolCountThresholdForHard {
		if _, ok := c.cfg.Workers["hard"]; ok {
			return "hard", "tool count > tool_count_threshold_for_hard"
		}
	}

	if len(tiers) > 1 {
		sortedTiers := append([]string(nil), tiers...)
		sort.Strings(sortedTiers)
		staticPick := sortedTiers[0]

		// Phase 5: ambiguous-multi-tier is the only path where the assist
		// LLM gets to vote. If it returns one of the available tiers, use it;
		// otherwise fall back to the static alphabetical pick. The assist
		// has its own internal timeout so this can't hang the request.
		if c.assist != nil {
			lastUser := lastUserMessage(req)
			if lastUser != "" {
				dec := c.assist.Decide(context.Background(), lastUser)
				if dec.Tier != "" && containsString(sortedTiers, dec.Tier) {
					return dec.Tier, "assist LLM voted (" + dec.Latency.Truncate(1e6).String() +
						"; raw=" + dec.Raw + ")"
				}
				// Failure / unparseable / tier not available — fall through.
			}
		}
		return staticPick, "model hosted on multiple tiers; deterministic alphabetical pick"
	}

	return c.cfg.Routing.DefaultTier, "model not hosted on any tier; using routing.default_tier"
}

// lastUserMessage returns the most recent role="user" message body, or "".
// Used to feed the assist LLM the most-relevant slice of the conversation.
func lastUserMessage(req *ChatRequest) string {
	for i := len(req.Messages) - 1; i >= 0; i-- {
		if req.Messages[i].Role == "user" {
			return req.Messages[i].Content
		}
	}
	return ""
}

func containsString(haystack []string, needle string) bool {
	for _, s := range haystack {
		if s == needle {
			return true
		}
	}
	return false
}

// estimateTokens — rough heuristic: 1 token ≈ 4 chars of message content.
// Good enough for routing; we don't need tokenizer-grade accuracy here.
func estimateTokens(req *ChatRequest) int {
	total := 0
	for _, m := range req.Messages {
		total += len(m.Content) / 4
	}
	return total
}
