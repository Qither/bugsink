#!/bin/bash
set -euo pipefail

usage() {
    cat <<'EOF'
Usage: ./builddocker.bash [wheel-file] [--publish] [--validate]

Environment:
  BUGSINK_IMAGE_REPO      Image repository to tag. Default: bugsink-fork
  BUGSINK_IMAGE_TAG       Image tag. Default: version from wheel, with + changed to -
  BUGSINK_PUBLISH=1       Push after a successful build.
  BUGSINK_VALIDATE=1      Inspect the image after build/publish.
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

echo "Building Bugsink image from wheel: $WHEEL_FILE"
echo "Image: $IMAGE_REF"

docker build -f Dockerfile.fromwheel --build-arg WHEEL_FILE="$WHEEL_FILE" -t "$IMAGE_REF" .

if [ "$VALIDATE" = "1" ]; then
    docker image inspect "$IMAGE_REF" >/dev/null
    echo "Validated local image: $IMAGE_REF"
fi

if [ "$PUBLISH" = "1" ]; then
    if [ -z "${BUGSINK_IMAGE_REPO:-}" ]; then
        echo "Refusing to publish without BUGSINK_IMAGE_REPO. Set it to your fork registry/repository." >&2
        exit 1
    fi
    docker push "$IMAGE_REF"
    docker buildx imagetools inspect "$IMAGE_REF" >/dev/null
    echo "Published image: $IMAGE_REF"
else
    echo "Build complete. Publish explicitly with BUGSINK_PUBLISH=1 BUGSINK_IMAGE_REPO=<registry/repo> $0 $WHEEL_FILE"
fi
