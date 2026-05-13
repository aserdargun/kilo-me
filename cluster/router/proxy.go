package main

import (
	"bytes"
	"encoding/json"
	"io"
	"log"
	"net/http"
	"net/http/httputil"
	"net/url"
	"time"
)

type Proxy struct {
	pool       *WorkerPool
	classifier *Classifier
}

func NewProxy(pool *WorkerPool, c *Classifier) *Proxy {
	return &Proxy{pool: pool, classifier: c}
}

// ChatHandler — primary handler for /v1/chat/completions.
//
//   1. Read & parse the body (keep the raw bytes around for the forward).
//   2. Classify into a tier via the model field / size / tool count.
//   3. If the chosen tier is unhealthy, pick any other healthy tier.
//   4. Forward via httputil.ReverseProxy — streaming is preserved natively.
func (p *Proxy) ChatHandler(w http.ResponseWriter, r *http.Request) {
	start := time.Now()

	body, err := io.ReadAll(r.Body)
	r.Body.Close()
	if err != nil {
		http.Error(w, "read body: "+err.Error(), http.StatusBadRequest)
		return
	}

	var req ChatRequest
	if err := json.Unmarshal(body, &req); err != nil {
		http.Error(w, "parse request: "+err.Error(), http.StatusBadRequest)
		return
	}

	tier := p.classifier.Classify(&req)
	originalTier := tier
	if !p.pool.IsHealthy(tier) {
		alt := p.pool.PickAlternativeTier(tier)
		if alt == "" {
			log.Printf("[chat] model=%s tier=%s ALL_WORKERS_DOWN", req.Model, tier)
			writeError(w, http.StatusServiceUnavailable,
				"all cluster workers unavailable; client should walk Rule 06 fallback chain")
			return
		}
		log.Printf("[chat] model=%s tier=%s→%s (failover)", req.Model, tier, alt)
		tier = alt
	}

	target, err := url.Parse(p.pool.WorkerURL(tier))
	if err != nil {
		writeError(w, http.StatusInternalServerError, "bad worker URL: "+err.Error())
		return
	}

	rp := httputil.NewSingleHostReverseProxy(target)
	// httputil's default director already swaps Scheme+Host; we add the
	// auth-header strip and the X-Kilo-Tier observability header.
	originalDirector := rp.Director
	rp.Director = func(req *http.Request) {
		originalDirector(req)
		req.Header.Del("Authorization")
		req.Host = target.Host
	}
	rp.ModifyResponse = func(resp *http.Response) error {
		resp.Header.Set("X-Kilo-Tier", tier)
		if tier != originalTier {
			resp.Header.Set("X-Kilo-Failover", "1")
		}
		return nil
	}
	rp.ErrorHandler = func(rw http.ResponseWriter, _ *http.Request, err error) {
		log.Printf("[chat] proxy error tier=%s: %v", tier, err)
		writeError(rw, http.StatusBadGateway, "upstream worker error: "+err.Error())
	}

	// Replay the body to the upstream.
	r.Body = io.NopCloser(bytes.NewReader(body))
	r.ContentLength = int64(len(body))

	rp.ServeHTTP(w, r)

	log.Printf("[chat] model=%s tier=%s elapsed=%s tokens≈%d tools=%d",
		req.Model, tier, time.Since(start).Truncate(time.Millisecond),
		estimateTokens(&req), len(req.Tools))
}

// OllamaPassthrough forwards /api/* to the classified tier without parsing
// the body. Used for endpoints that aren't OpenAI-shape (e.g. /api/tags).
// Falls back to the default tier when classification isn't possible.
func (p *Proxy) OllamaPassthrough(w http.ResponseWriter, r *http.Request) {
	tier := p.classifier.cfg.Routing.DefaultTier
	if !p.pool.IsHealthy(tier) {
		alt := p.pool.PickAlternativeTier(tier)
		if alt == "" {
			writeError(w, http.StatusServiceUnavailable, "all cluster workers unavailable")
			return
		}
		tier = alt
	}
	target, err := url.Parse(p.pool.WorkerURL(tier))
	if err != nil {
		writeError(w, http.StatusInternalServerError, "bad worker URL")
		return
	}
	rp := httputil.NewSingleHostReverseProxy(target)
	original := rp.Director
	rp.Director = func(req *http.Request) {
		original(req)
		req.Header.Del("Authorization")
		req.Host = target.Host
	}
	rp.ServeHTTP(w, r)
}

// ClassifyHandler — dry-run classification. Accepts the same JSON body as
// /v1/chat/completions, returns the tier the router would forward to plus
// metadata. Does NOT forward the request to any worker.
//
// Response shape:
//
//	{
//	  "tier":           "hard",
//	  "reason":         "model hosted on exactly one tier",
//	  "healthy":        true,
//	  "fallback_tier":  "soft",      // chosen if `tier` is unhealthy
//	  "model":          "...",
//	  "estimated_tokens": 123,
//	  "tool_count":    3
//	}
func (p *Proxy) ClassifyHandler(w http.ResponseWriter, r *http.Request) {
	body, err := io.ReadAll(r.Body)
	r.Body.Close()
	if err != nil {
		writeError(w, http.StatusBadRequest, "read body: "+err.Error())
		return
	}
	var req ChatRequest
	if err := json.Unmarshal(body, &req); err != nil {
		writeError(w, http.StatusBadRequest, "parse request: "+err.Error())
		return
	}
	tier, reason := p.classifier.ClassifyWithReason(&req)
	healthy := p.pool.IsHealthy(tier)
	fallback := ""
	if !healthy {
		fallback = p.pool.PickAlternativeTier(tier)
	}
	w.Header().Set("Content-Type", "application/json")
	_ = json.NewEncoder(w).Encode(map[string]any{
		"tier":             tier,
		"reason":           reason,
		"healthy":          healthy,
		"fallback_tier":    fallback,
		"model":            req.Model,
		"estimated_tokens": estimateTokens(&req),
		"tool_count":       len(req.Tools),
	})
}

// ModelsHandler — returns the union of all worker models in OpenAI shape.
// Doesn't require any worker to be live; reads straight from config.
func (p *Proxy) ModelsHandler(w http.ResponseWriter, r *http.Request) {
	type model struct {
		ID     string `json:"id"`
		Object string `json:"object"`
		Owned  string `json:"owned_by"`
	}
	seen := map[string]bool{}
	models := []model{}
	for _, tier := range p.classifier.cfg.WorkerTiers() {
		for _, m := range p.classifier.cfg.Workers[tier].Models {
			if !seen[m] {
				seen[m] = true
				models = append(models, model{ID: m, Object: "model", Owned: tier})
			}
		}
	}
	w.Header().Set("Content-Type", "application/json")
	_ = json.NewEncoder(w).Encode(map[string]any{
		"object": "list",
		"data":   models,
	})
}

// HealthHandler — diagnostic. No auth (so monitoring can scrape without
// the shared token).
func (p *Proxy) HealthHandler(w http.ResponseWriter, r *http.Request) {
	w.Header().Set("Content-Type", "application/json")
	_ = json.NewEncoder(w).Encode(map[string]any{
		"workers": p.pool.Status(),
		"ts":      time.Now().UTC().Format(time.RFC3339),
	})
}

func writeError(w http.ResponseWriter, status int, msg string) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(map[string]any{
		"error": map[string]any{
			"message": msg,
			"type":    "kilo_router_error",
			"code":    status,
		},
	})
}
