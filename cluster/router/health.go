package main

import (
	"context"
	"log"
	"net/http"
	"sync"
	"time"
)

// WorkerPool tracks live/dead state per tier. State is updated by a
// background loop polling each worker's /api/tags every 5 s with a 3 s
// per-request timeout. Reads are RW-mutex protected.
type WorkerPool struct {
	cfg *Config

	mu        sync.RWMutex
	healthy   map[string]bool
	lastErr   map[string]string
	lastCheck map[string]time.Time
}

func NewWorkerPool(cfg *Config) *WorkerPool {
	p := &WorkerPool{
		cfg:       cfg,
		healthy:   make(map[string]bool),
		lastErr:   make(map[string]string),
		lastCheck: make(map[string]time.Time),
	}
	// Initialize all tiers as unknown (false) until first poll completes.
	for tier := range cfg.Workers {
		p.healthy[tier] = false
		p.lastErr[tier] = "not yet polled"
	}
	return p
}

func (p *WorkerPool) StartHealthLoop(ctx context.Context) {
	// First poll runs synchronously so /healthz returns real data immediately.
	p.pollAll()

	ticker := time.NewTicker(5 * time.Second)
	defer ticker.Stop()

	for {
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
			p.pollAll()
		}
	}
}

func (p *WorkerPool) pollAll() {
	var wg sync.WaitGroup
	for tier, w := range p.cfg.Workers {
		wg.Add(1)
		go func(tier, url string) {
			defer wg.Done()
			p.pollOne(tier, url)
		}(tier, w.URL)
	}
	wg.Wait()
}

func (p *WorkerPool) pollOne(tier, url string) {
	client := http.Client{Timeout: 3 * time.Second}
	req, err := http.NewRequest("GET", url+"/api/tags", nil)
	if err != nil {
		p.setUnhealthy(tier, "new request: "+err.Error())
		return
	}
	resp, err := client.Do(req)
	if err != nil {
		p.setUnhealthy(tier, err.Error())
		return
	}
	defer resp.Body.Close()
	if resp.StatusCode >= 500 {
		p.setUnhealthy(tier, "HTTP "+resp.Status)
		return
	}
	p.setHealthy(tier)
}

func (p *WorkerPool) setHealthy(tier string) {
	p.mu.Lock()
	defer p.mu.Unlock()
	if !p.healthy[tier] {
		log.Printf("worker %q transitioned: down → up", tier)
	}
	p.healthy[tier] = true
	p.lastErr[tier] = ""
	p.lastCheck[tier] = time.Now()
}

func (p *WorkerPool) setUnhealthy(tier, reason string) {
	p.mu.Lock()
	defer p.mu.Unlock()
	if p.healthy[tier] {
		log.Printf("worker %q transitioned: up → down (%s)", tier, reason)
	}
	p.healthy[tier] = false
	p.lastErr[tier] = reason
	p.lastCheck[tier] = time.Now()
}

func (p *WorkerPool) IsHealthy(tier string) bool {
	p.mu.RLock()
	defer p.mu.RUnlock()
	return p.healthy[tier]
}

func (p *WorkerPool) WorkerURL(tier string) string {
	return p.cfg.Workers[tier].URL
}

// PickAlternativeTier returns any tier OTHER than `current` that is currently
// healthy, or empty string if none. Used by the proxy for inline failover
// when the classifier's first pick is dead.
func (p *WorkerPool) PickAlternativeTier(current string) string {
	p.mu.RLock()
	defer p.mu.RUnlock()
	for tier, healthy := range p.healthy {
		if healthy && tier != current {
			return tier
		}
	}
	return ""
}

// Status — JSON-friendly snapshot for the /healthz endpoint.
func (p *WorkerPool) Status() map[string]map[string]any {
	p.mu.RLock()
	defer p.mu.RUnlock()
	out := make(map[string]map[string]any)
	for tier := range p.cfg.Workers {
		var lastCheckStr string
		if t, ok := p.lastCheck[tier]; ok {
			lastCheckStr = t.UTC().Format(time.RFC3339)
		}
		out[tier] = map[string]any{
			"healthy":    p.healthy[tier],
			"url":        p.cfg.Workers[tier].URL,
			"last_error": p.lastErr[tier],
			"last_check": lastCheckStr,
		}
	}
	return out
}
