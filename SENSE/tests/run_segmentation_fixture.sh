#!/usr/bin/env bash
set -euo pipefail

repo="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$repo"
case "$(uname -s)" in
    MINGW*|MSYS*) export MSYS_NO_PATHCONV=1 ;;
esac
compose=(docker compose -p pace-seg-fixture -f SENSE/tests/compose.segmentation.yaml)
runner=(python /repo/SENSE/tests/segmentation_fixture.py)

case "${1:-verify}" in
    verify)
        "${compose[@]}" build receiver pace-web fixture-worker
        "${compose[@]}" run --rm --no-deps fixture-runner "${runner[@]}" prepare
        "${compose[@]}" up -d --wait
        "${compose[@]}" exec -T pace-web python manage.py test data_cube
        "${compose[@]}" run --rm fixture-runner python -m unittest discover -s SENSE/tests
        bash SENSE/tests/run_segmentation_fixture.sh replay-synthetic
        ;;
    replay-synthetic)
        inputs=/fixtures
        session="$("${compose[@]}" run --rm fixture-runner "${runner[@]}" replay \
            --jpeg "$inputs/source.jpg" --mask "$inputs/mask.png" \
            --manifest "$inputs/manifest.json" --expected "$inputs/expected.json")"
        "${compose[@]}" exec -T pace-web python manage.py render_segmentation_overlay "$session" 0 /fixture-output/overlay.png
        "${compose[@]}" run --rm fixture-runner "${runner[@]}" check-overlay --expected "$inputs/expected.json"
        ;;
    replay)
        inputs="$(cd "${2:?Provide a directory containing source.jpg, mask.png, manifest.json and expected.json}" && pwd)"
        if command -v cygpath >/dev/null; then
            inputs="$(cygpath -am "$inputs")"
        fi
        session="$("${compose[@]}" run --rm -v "$inputs:/replay:ro" fixture-runner "${runner[@]}" replay \
            --jpeg /replay/source.jpg --mask /replay/mask.png \
            --manifest /replay/manifest.json --expected /replay/expected.json)"
        "${compose[@]}" exec -T pace-web python manage.py render_segmentation_overlay "$session" 0 /fixture-output/overlay.png
        "${compose[@]}" run --rm -v "$inputs:/replay:ro" fixture-runner "${runner[@]}" check-overlay --expected /replay/expected.json
        ;;
    *)
        echo "Usage: $0 {verify|replay DIR}" >&2
        exit 2
        ;;
esac
