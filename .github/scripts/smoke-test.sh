#!/usr/bin/env bash
# Starts an image as the service runs it, through its entrypoint and as a non-root user,
# and waits for /healthz to answer. No DEE is mounted, so /healthz reports it missing;
# the service still has to start.
#
#   smoke-test.sh IMAGE
set -euo pipefail

image=${1:?usage: smoke-test.sh IMAGE}
port=18080

id=$(docker run -d --user 1000:1000 -p "127.0.0.1:$port:8080" "$image")
trap 'docker rm -f "$id" >/dev/null' EXIT

for _ in $(seq 60); do
  if body=$(curl -fsS --max-time 10 "http://127.0.0.1:$port/healthz" 2>/dev/null); then
    if grep -q '"ok":true' <<<"$body"; then
      echo "healthz ok: ${body:0:300}"
      exit 0
    fi
    echo "::error::/healthz answered without \"ok\":true: $body"
    exit 1
  fi
  if [ "$(docker inspect -f '{{.State.Running}}' "$id")" != true ]; then
    echo "::error::The container exited before /healthz answered."
    docker logs "$id"
    exit 1
  fi
  sleep 2
done

echo "::error::/healthz did not answer within 120 seconds."
docker logs "$id"
exit 1
