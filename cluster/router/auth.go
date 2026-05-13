package main

import (
	"crypto/subtle"
	"net/http"
	"strings"
)

// AuthMiddleware enforces a shared bearer token. Comparison is
// constant-time to avoid leaking the secret via response-timing.
func AuthMiddleware(token string, next http.HandlerFunc) http.HandlerFunc {
	tokenBytes := []byte(token)
	return func(w http.ResponseWriter, r *http.Request) {
		h := r.Header.Get("Authorization")
		if !strings.HasPrefix(h, "Bearer ") {
			http.Error(w, "missing Bearer token", http.StatusUnauthorized)
			return
		}
		provided := strings.TrimPrefix(h, "Bearer ")
		if subtle.ConstantTimeCompare([]byte(provided), tokenBytes) != 1 {
			http.Error(w, "bad token", http.StatusUnauthorized)
			return
		}
		next(w, r)
	}
}
