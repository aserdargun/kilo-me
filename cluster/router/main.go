// kilo-router — Phase 3 Pi-hosted cluster router for kilo-me.
//
// Accepts OpenAI-compatible /v1/chat/completions requests, classifies them
// (rules-based), and forwards to the right Ollama worker over Tailscale.
// Streams responses back unchanged. Cluster failover is handled inline:
// if the chosen tier is unhealthy, traffic switches to the other tier.
//
// This is the only stateful service running on the Pi. ~20 MB RSS, sub-ms
// added latency, single static binary.
package main

import (
	"context"
	"flag"
	"log"
	"net/http"
	"os"
	"os/signal"
	"syscall"
	"time"
)

func main() {
	configPath := flag.String("config", "/etc/kilo-router/config.yaml", "path to YAML config")
	flag.Parse()

	cfg, err := LoadConfig(*configPath)
	if err != nil {
		log.Fatalf("config: %v", err)
	}

	pool := NewWorkerPool(cfg)
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	go pool.StartHealthLoop(ctx)

	classifier := NewClassifier(cfg)
	if assistant := NewAssistant(cfg.RouterAssist); assistant != nil {
		classifier.SetAssistant(assistant)
		log.Printf("router-assist enabled: model=%s url=%s timeout=%dms",
			cfg.RouterAssist.Model, cfg.RouterAssist.URL, cfg.RouterAssist.TimeoutMs)
	}
	proxy := NewProxy(pool, classifier)

	mux := http.NewServeMux()
	mux.HandleFunc("/healthz", proxy.HealthHandler)
	mux.HandleFunc("/v1/chat/completions", AuthMiddleware(cfg.Auth.Token, proxy.ChatHandler))
	mux.HandleFunc("/v1/models", AuthMiddleware(cfg.Auth.Token, proxy.ModelsHandler))
	// /v1/classify — dry-run the classifier on a chat-completion body and
	// return {tier, healthy, fallback_tier, reason}. Source of truth for the
	// cluster-health MCP server's route_for() tool.
	mux.HandleFunc("/v1/classify", AuthMiddleware(cfg.Auth.Token, proxy.ClassifyHandler))
	// Bare-Ollama passthrough (some clients hit /api/* directly).
	mux.HandleFunc("/api/tags", AuthMiddleware(cfg.Auth.Token, proxy.OllamaPassthrough))
	mux.HandleFunc("/api/chat", AuthMiddleware(cfg.Auth.Token, proxy.OllamaPassthrough))

	srv := &http.Server{
		Addr:              cfg.Server.Listen,
		Handler:           mux,
		ReadHeaderTimeout: 30 * time.Second,
		// No write timeout — streaming responses can take minutes for long
		// completions. Idle timeout is enough to reap dead connections.
		IdleTimeout: 5 * time.Minute,
	}

	go func() {
		log.Printf("kilo-router listening on %s; workers: %v", cfg.Server.Listen, cfg.WorkerTiers())
		if err := srv.ListenAndServe(); err != nil && err != http.ErrServerClosed {
			log.Fatalf("listen: %v", err)
		}
	}()

	sigChan := make(chan os.Signal, 1)
	signal.Notify(sigChan, syscall.SIGINT, syscall.SIGTERM)
	<-sigChan

	log.Print("shutting down…")
	shutdownCtx, shutdownCancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer shutdownCancel()
	if err := srv.Shutdown(shutdownCtx); err != nil {
		log.Printf("shutdown error: %v", err)
	}
}
