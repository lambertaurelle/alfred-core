#!/usr/bin/env bash
# Idempotent V8.0 Bootstrap for Alfred Chief of Staff Mono-Repo
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ALFRED_ROOT="${ALFRED_ROOT_DIR:-$(cd "${SCRIPT_DIR}/.." && pwd)}"
export ALFRED_ROOT_DIR="${ALFRED_ROOT}"

SKIP_PIP=false
DO_UPGRADE=false
DO_ONBOARD=false

for arg in "$@"; do
    case "${arg}" in
        --skip-pip)
            SKIP_PIP=true
            ;;
        --upgrade)
            DO_UPGRADE=true
            ;;
        --onboard)
            DO_ONBOARD=true
            ;;
        *)
            echo "Unknown option: ${arg} (supported: --skip-pip, --upgrade, --onboard)" >&2
            exit 2
            ;;
    esac
done

echo "[1/4] Preparing gitignored private instance/ directory tree..."
mkdir -p \
    "${ALFRED_ROOT}/instance/config" \
    "${ALFRED_ROOT}/instance/secrets/signature_vault" \
    "${ALFRED_ROOT}/instance/data/knowledge" \
    "${ALFRED_ROOT}/instance/data/cache" \
    "${ALFRED_ROOT}/instance/data/state" \
    "${ALFRED_ROOT}/instance/extensions"
chmod 700 "${ALFRED_ROOT}/instance/secrets" "${ALFRED_ROOT}/instance/secrets/signature_vault"

if [[ ! -f "${ALFRED_ROOT}/instance/config/settings.yaml" ]]; then
    cp "${ALFRED_ROOT}/core/config/settings.example.yaml" "${ALFRED_ROOT}/instance/config/settings.yaml"
    echo "  -> Created instance/config/settings.yaml from template."
else
    echo "  -> Existing instance/config/settings.yaml preserved."
fi

echo "[2/4] Checking Python environment (.venv)..."
if [[ ! -x "${ALFRED_ROOT}/.venv/bin/python" ]]; then
    python3 -m venv "${ALFRED_ROOT}/.venv"
fi
if [[ "${SKIP_PIP}" != true ]]; then
    if [[ ! -x "${ALFRED_ROOT}/.venv/bin/alfred" || "${DO_UPGRADE}" == true ]]; then
        "${ALFRED_ROOT}/.venv/bin/pip" install --quiet -e "${ALFRED_ROOT}[dev]"
    fi
fi

echo "[3/4] Enabling local Git hooks (.githooks) and .agents symlinks..."
if [[ -d "${ALFRED_ROOT}/.git" && -d "${ALFRED_ROOT}/.githooks" ]]; then
    git -C "${ALFRED_ROOT}" config core.hooksPath .githooks
fi
for ws_dir in "${ALFRED_ROOT}"/core/workspaces/alfred-*; do
    [[ -d "${ws_dir}" ]] || continue
    if [[ ! -e "${ws_dir}/.agents" && ! -L "${ws_dir}/.agents" ]]; then
        ln -s ../../../.agents "${ws_dir}/.agents"
    fi
done

if [[ "${DO_ONBOARD}" == true ]]; then
    echo "[4/4] Launching GCP & OAuth2 onboarding wizard (alfred bootstrap-gcp --finish-setup)..."
    "${ALFRED_ROOT}/.venv/bin/alfred" bootstrap-gcp --finish-setup
else
    echo "[4/4] Alfred V8.0 bootstrap complete."
    echo "Next steps:"
    echo "  1. ${ALFRED_ROOT}/.venv/bin/alfred bootstrap-gcp   (or ./scripts/bootstrap.sh --onboard)"
    echo "  2. ${ALFRED_ROOT}/.venv/bin/alfred status-onboarding --json"
    echo "  3. ${ALFRED_ROOT}/.venv/bin/alfred init-workspaces --prune-legacy"
    echo "  4. Open the 'alfred-onboard' workspace in Antigravity Web."
fi
