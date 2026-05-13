# Multi-stage build for the kilo-me cluster router.
#
# Result: a ~10 MB scratch image with a single static binary, suitable for
# the Pi 5's arm64 runtime. Build with:
#
#   docker buildx build --platform linux/arm64 -t kilo-router:latest --load .
#
# setup-pi.sh prefers a native `go build` over Docker (faster on the Pi and
# avoids one more daemon), but this Dockerfile exists for users who already
# run their Pi as a Docker host.

FROM --platform=$BUILDPLATFORM golang:1.22-alpine AS build
WORKDIR /src

# Cache deps separately from source.
COPY go.mod go.sum ./
RUN go mod download

COPY . .

ARG TARGETOS TARGETARCH
RUN CGO_ENABLED=0 GOOS=${TARGETOS:-linux} GOARCH=${TARGETARCH:-arm64} \
    go build -trimpath -ldflags="-s -w" -o /out/kilo-router .

FROM scratch
COPY --from=build /out/kilo-router /kilo-router
EXPOSE 8080
ENTRYPOINT ["/kilo-router"]
CMD ["--config", "/etc/kilo-router/config.yaml"]
