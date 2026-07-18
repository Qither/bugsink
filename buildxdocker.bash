#!/bin/bash
set -euo pipefail

usage() {
    cat <<'EOF'
Usage: ./buildxdocker.bash [wheel-file] [--publish] [--validate]

Environment:
  BUGSINK_IMAGE_REPO      Image repository to tag. Required for publish.
  BUGSINK_IMAGE_TAG       Image tag. Default: version from wheel, with + changed to -
  BUGSINK_PLATFORMS       Publish platforms. Default: linux/amd64,linux/arm64
  BUGSINK_PUBLISH=1       Push a multi-platform image after build.
  BUGSINK_VALIDATE=1      Inspect the local image or registry manifest after build.

Without registry credentials, the script falls back to a local linux/amd64 image.
EOF
}

PUBLISH="${BUGSINK_PUBLISH:-0}"
VALIDATE="${BUGSINK_VALIDATE:-0}"
WHEEL_FILE=""
START_DIR=$(pwd)
SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)

resolve_wheel_file() {
    local input="$1"
    local candidate="$input"
    if [[ "$candidate" != /* ]]; then
        candidate="$START_DIR/$candidate"
    fi
    if [ ! -f "$candidate" ]; then
        echo "Wheel file does not exist: $input" >&2
        exit 1
    fi

    local wheel_dir
    wheel_dir=$(cd "$(dirname "$candidate")" && pwd -P)
    if [ "$wheel_dir" != "$SCRIPT_DIR/dist" ]; then
        echo "Wheel file must be inside bugsink/dist: $input" >&2
        exit 1
    fi
    basename "$candidate"
}

for arg in "$@"; do
    case "$arg" in
        --publish) PUBLISH=1 ;;
        --validate) VALIDATE=1 ;;
        --help|-h) usage; exit 0 ;;
        *)
            if [ -n "$WHEEL_FILE" ]; then
                echo "Unexpected argument: $arg" >&2
                usage >&2
                exit 2
            fi
            WHEEL_FILE=$(resolve_wheel_file "$arg")
            ;;
    esac
done

cd "$SCRIPT_DIR"

if [ -z "$WHEEL_FILE" ]; then
    WHEEL_FILE=$(find dist -maxdepth 1 -type f -name '*bugsink*.whl' -printf '%T@ %f\n' 2>/dev/null | sort -n | tail -n1 | cut -d' ' -f2-)
fi
if [ -z "$WHEEL_FILE" ]; then
    echo "No Bugsink wheel found. Pass a wheel file or build one into dist/." >&2
    exit 1
fi

VERSION=$(echo "$WHEEL_FILE" | cut -d'-' -f2)
DEFAULT_TAG=${VERSION//+/-}
IMAGE_REPO="${BUGSINK_IMAGE_REPO:-bugsink-fork}"
IMAGE_TAG="${BUGSINK_IMAGE_TAG:-$DEFAULT_TAG}"
IMAGE_REF="${IMAGE_REPO}:${IMAGE_TAG}"
PLATFORMS="${BUGSINK_PLATFORMS:-linux/amd64,linux/arm64}"

echo "Building Bugsink image from wheel: $WHEEL_FILE"
echo "Image: $IMAGE_REF"

if [ "$PUBLISH" = "1" ]; then
    if [ -z "${BUGSINK_IMAGE_REPO:-}" ]; then
        echo "No BUGSINK_IMAGE_REPO set; falling back to local linux/amd64 build instead of publishing."
        docker buildx build -f Dockerfile.fromwheel --platform linux/amd64 --build-arg WHEEL_FILE="$WHEEL_FILE" -t "$IMAGE_REF" --load .
    else
        if docker buildx build -f Dockerfile.fromwheel --platform "$PLATFORMS" --build-arg WHEEL_FILE="$WHEEL_FILE" -t "$IMAGE_REF" --push .; then
            docker buildx imagetools inspect "$IMAGE_REF" >/dev/null
            echo "Published image: $IMAGE_REF"
        else
            echo "Publish failed; falling back to a local linux/amd64 image for credential-free deployment."
            docker buildx build -f Dockerfile.fromwheel --platform linux/amd64 --build-arg WHEEL_FILE="$WHEEL_FILE" -t "$IMAGE_REF" --load .
        fi
    fi
else
    docker buildx build -f Dockerfile.fromwheel --platform linux/amd64 --build-arg WHEEL_FILE="$WHEEL_FILE" -t "$IMAGE_REF" --load .
    echo "Local build complete. Publish explicitly with BUGSINK_PUBLISH=1 BUGSINK_IMAGE_REPO=<registry/repo> $0 $WHEEL_FILE"
fi

if [ "$VALIDATE" = "1" ]; then
    docker image inspect "$IMAGE_REF" >/dev/null || docker buildx imagetools inspect "$IMAGE_REF" >/dev/null
    echo "Validated image: $IMAGE_REF"
fi
