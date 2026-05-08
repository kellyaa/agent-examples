#!/usr/bin/env bash
#
# Build the stateful_skill_demo agent image from its Dockerfile and load it
# into a local Kind cluster so the in-cluster Deployment can pull it
# without needing a registry.
#
# Usage:
#   ./build-and-load.sh                     # defaults: image=stateful-skill-demo:latest, cluster=kagenti
#   IMAGE=myimg:dev CLUSTER_NAME=kind ./build-and-load.sh
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

IMAGE="${IMAGE:-localhost/stateful-skill-demo:latest}"
CLUSTER_NAME="${CLUSTER_NAME:-kagenti}"
CONTEXT_DIR="${CONTEXT_DIR:-$SCRIPT_DIR}"
DOCKERFILE="${DOCKERFILE:-$CONTEXT_DIR/Dockerfile}"

GREEN='\033[0;32m'
RED='\033[0;31m'
NC='\033[0m'
log()  { echo -e "${GREEN}>>>${NC} $*"; }
die()  { echo -e "${RED}!!!${NC} $*" >&2; exit 1; }

command -v docker >/dev/null 2>&1 || die "docker not found in PATH"
command -v kind   >/dev/null 2>&1 || die "kind not found in PATH"
[ -f "$DOCKERFILE" ] || die "Dockerfile not found at $DOCKERFILE"

kind get clusters 2>/dev/null | grep -qx "$CLUSTER_NAME" \
    || die "Kind cluster '$CLUSTER_NAME' not found. Create it first or set CLUSTER_NAME=<name>."

log "Building $IMAGE from $DOCKERFILE (context: $CONTEXT_DIR)..."
docker build -f "$DOCKERFILE" -t "$IMAGE" "$CONTEXT_DIR"

log "Loading $IMAGE into Kind cluster '$CLUSTER_NAME'..."
kind load docker-image "$IMAGE" --name "$CLUSTER_NAME"

log "Done. Image '$IMAGE' is available inside the cluster."
log "Verify with: docker exec ${CLUSTER_NAME}-control-plane crictl images | grep stateful-skill-demo"
