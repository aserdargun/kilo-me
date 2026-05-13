package main

import (
	"fmt"
	"os"
	"sort"

	"gopkg.in/yaml.v3"
)

// Config is the on-disk YAML the router reads at startup.
type Config struct {
	Server struct {
		Listen string `yaml:"listen"` // default ":8080"
	} `yaml:"server"`

	Auth struct {
		Token string `yaml:"token"` // shared bearer token
	} `yaml:"auth"`

	// Workers keyed by tier name. "soft" and "hard" are the convention but
	// any string is allowed — the model→tier map is built from this section.
	Workers map[string]Worker `yaml:"workers"`

	Routing Routing `yaml:"routing"`

	// RouterAssist is the optional 1 B-class LLM running on the Pi itself.
	// It is consulted ONLY when the static classifier would fall back to the
	// alphabetical pick (rule 4) — i.e. the request could go either way.
	// On timeout or error the static answer is used; the assist is strictly
	// best-effort. Disabled by default.
	RouterAssist RouterAssist `yaml:"router_assist"`
}

type RouterAssist struct {
	Enabled   bool   `yaml:"enabled"`     // default: false
	URL       string `yaml:"url"`         // e.g. http://127.0.0.1:11434
	Model     string `yaml:"model"`       // e.g. qwen2.5:1.5b-instruct-q4_K_M
	TimeoutMs int    `yaml:"timeout_ms"`  // hard cap; default 1500
	MaxChars  int    `yaml:"max_chars"`   // truncate the user message; default 2000
}

type Worker struct {
	URL    string   `yaml:"url"`    // e.g. http://macmini:11434
	Models []string `yaml:"models"` // Ollama tags hosted on this worker
}

type Routing struct {
	// Token estimate above which we prefer the hard tier.
	TokenThresholdForHard int `yaml:"token_threshold_for_hard"`

	// Tool-array length above which we prefer the hard tier.
	ToolCountThresholdForHard int `yaml:"tool_count_threshold_for_hard"`

	// Where to land requests we couldn't otherwise classify. Must match
	// one of the keys in Workers (or be empty → use the first declared).
	DefaultTier string `yaml:"default_tier"`
}

func LoadConfig(path string) (*Config, error) {
	data, err := os.ReadFile(path)
	if err != nil {
		return nil, fmt.Errorf("read %s: %w", path, err)
	}
	var cfg Config
	if err := yaml.Unmarshal(data, &cfg); err != nil {
		return nil, fmt.Errorf("parse %s: %w", path, err)
	}

	// Defaults.
	if cfg.Server.Listen == "" {
		cfg.Server.Listen = ":8080"
	}
	if cfg.Routing.TokenThresholdForHard == 0 {
		cfg.Routing.TokenThresholdForHard = 8000
	}
	if cfg.Routing.ToolCountThresholdForHard == 0 {
		cfg.Routing.ToolCountThresholdForHard = 5
	}

	// Validation.
	if len(cfg.Workers) == 0 {
		return nil, fmt.Errorf("config has no workers")
	}
	if cfg.Auth.Token == "" {
		return nil, fmt.Errorf("config has empty auth.token; refusing to start without a shared secret")
	}
	for tier, w := range cfg.Workers {
		if w.URL == "" {
			return nil, fmt.Errorf("worker %q has empty url", tier)
		}
		if len(w.Models) == 0 {
			return nil, fmt.Errorf("worker %q has no models declared", tier)
		}
	}
	if cfg.Routing.DefaultTier == "" {
		// Pick the first tier alphabetically so behavior is deterministic
		// across restarts.
		cfg.Routing.DefaultTier = cfg.WorkerTiers()[0]
	}
	if _, ok := cfg.Workers[cfg.Routing.DefaultTier]; !ok {
		return nil, fmt.Errorf("routing.default_tier %q is not a declared worker", cfg.Routing.DefaultTier)
	}

	// RouterAssist defaults — only validated when enabled.
	if cfg.RouterAssist.TimeoutMs == 0 {
		cfg.RouterAssist.TimeoutMs = 1500
	}
	if cfg.RouterAssist.MaxChars == 0 {
		cfg.RouterAssist.MaxChars = 2000
	}
	if cfg.RouterAssist.Enabled {
		if cfg.RouterAssist.URL == "" {
			return nil, fmt.Errorf("router_assist.enabled=true but url is empty")
		}
		if cfg.RouterAssist.Model == "" {
			return nil, fmt.Errorf("router_assist.enabled=true but model is empty")
		}
	}

	return &cfg, nil
}

// WorkerTiers returns the sorted tier names for stable iteration.
func (c *Config) WorkerTiers() []string {
	out := make([]string, 0, len(c.Workers))
	for k := range c.Workers {
		out = append(out, k)
	}
	sort.Strings(out)
	return out
}
