#!/usr/bin/env bash
# Deterministic Zero-Leak Pre-Commit & History Guard for Alfred V8.0 Mono-Repo
# Generic patterns live here; instance-private terms load from gitignored instance/secrets/leak_terms.txt.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ALFRED_ROOT="${ALFRED_ROOT_DIR:-$(cd "${SCRIPT_DIR}/../.." && pwd)}"
TARGET_REPO="${1:-${ALFRED_ROOT}}"
cd "${TARGET_REPO}"

if ! git rev-parse --git-dir >/dev/null 2>&1; then
    echo "ERROR [Leak Guard]: ${TARGET_REPO} is not a Git repository." >&2
    exit 1
fi

VIOLATIONS=0

# 1. Check tracked, staged, and untracked non-ignored file paths against forbidden paths.
# Inside instance/, ONLY examples/, *.example.*, and extensions/{__init__.py,README.md,*.example.py} are allowed.
FORBIDDEN_PATH_REGEX='^(worktrees/|data/|secrets/|docs/history/|docs/architecture/swarm/|\.agents/agents/swarm-)|(^|/)(data/|secrets/|signature_vault/|client_secret|token\.json|mcp_auth\.json|leak_terms\.txt|.*\.pem$|.*\.key$|.*\.env$)'
ALLOWED_INSTANCE_REGEX='^instance/(examples/.+|config/[^/]+\.example\.(ya?ml|md|json)|config/prompts/[^/]+\.example\.md|extensions/(__init__\.py|README\.md|[^/]+\.example\.py))$'

check_forbidden_paths() {
    local path_list="$1"
    local bad=""
    while IFS= read -r p; do
        [[ -z "${p}" ]] && continue
        if printf '%s\n' "${p}" | grep -Eq "${FORBIDDEN_PATH_REGEX}"; then
            bad="${bad:+${bad}$'\n'}${p}"
        elif [[ "${p}" == instance/* ]] && ! printf '%s\n' "${p}" | grep -Eq "${ALLOWED_INSTANCE_REGEX}"; then
            bad="${bad:+${bad}$'\n'}${p}"
        fi
    done <<< "${path_list}"
    printf '%s' "${bad}"
}

TRACKED_AND_NEW="$( { git ls-files; git diff --cached --name-only; git ls-files --others --exclude-standard; } | sort -u )"

if [[ -n "${TRACKED_AND_NEW}" ]]; then
    BAD_PATHS="$(check_forbidden_paths "${TRACKED_AND_NEW}")"
    if [[ -n "${BAD_PATHS}" ]]; then
        echo "ERROR [Forbidden Tracked/Staged Paths]:" >&2
        echo "${BAD_PATHS}" >&2
        VIOLATIONS=$((VIOLATIONS + 1))
    fi
fi

# 2. Build composite sensitive content regex (bracketed so this script never matches itself)
LEAK_CONTENT_REGEX='(\b[1]92\.[1]68\.[0-9]{1,3}\.[0-9]{1,3}\b|\b[1]0\.[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}\b|\b[1]72\.(1[6-9]|2[0-9]|3[0-1])\.[0-9]{1,3}\.[0-9]{1,3}\b|\b[C]T [0-9]{1,4}\b|\b([0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}\b|@[g]mail\.com|@[o]utlook\.|@[h]otmail\.|@[y]ahoo\.|@[o]range\.fr|@[w]anadoo\.fr|@[f]ree\.fr|[B]EGIN [A-Z ]*PRIVATE KEY|[G]OCSPX-[0-9A-Za-z_-]+|[A]Iza[0-9A-Za-z_-]{35}|[y]a29\.[0-9A-Za-z_-]+|[g]hp_[0-9A-Za-z]{30,})'

# Append optional instance-private terms from gitignored instance/secrets/leak_terms.txt
PRIVATE_TERMS_FILE="${ALFRED_ROOT}/instance/secrets/leak_terms.txt"
EXTRA_CASE_INSENSITIVE_REGEX=""
if [[ -f "${PRIVATE_TERMS_FILE}" ]]; then
    while IFS= read -r term; do
        trimmed="${term#"${term%%[![:space:]]*}"}"
        trimmed="${trimmed%"${trimmed##*[![:space:]]}"}"
        [[ -z "${trimmed}" || "${trimmed}" == \#* ]] && continue
        if [[ -z "${EXTRA_CASE_INSENSITIVE_REGEX}" ]]; then
            EXTRA_CASE_INSENSITIVE_REGEX="(${trimmed}"
        else
            EXTRA_CASE_INSENSITIVE_REGEX="${EXTRA_CASE_INSENSITIVE_REGEX}|${trimmed}"
        fi
    done < "${PRIVATE_TERMS_FILE}"
    if [[ -n "${EXTRA_CASE_INSENSITIVE_REGEX}" ]]; then
        EXTRA_CASE_INSENSITIVE_REGEX="${EXTRA_CASE_INSENSITIVE_REGEX})"
    fi
fi

# 3. Scan working tree files (tracked + untracked non-ignored)
while IFS= read -r rel_file; do
    [[ -z "${rel_file}" ]] && continue
    [[ ! -f "${rel_file}" ]] && continue
    MATCHES="$(grep -InE "${LEAK_CONTENT_REGEX}" "${rel_file}" || true)"
    if [[ -n "${EXTRA_CASE_INSENSITIVE_REGEX}" ]]; then
        EXTRA_MATCHES="$(grep -IniE "${EXTRA_CASE_INSENSITIVE_REGEX}" "${rel_file}" || true)"
        [[ -n "${EXTRA_MATCHES}" ]] && MATCHES="${MATCHES:+${MATCHES}$'\n'}${EXTRA_MATCHES}"
    fi
    if [[ -n "${MATCHES}" ]]; then
        echo "ERROR [Sensitive Data in File '${rel_file}']:" >&2
        echo "${MATCHES}" >&2
        VIOLATIONS=$((VIOLATIONS + 1))
    fi
done <<< "${TRACKED_AND_NEW}"

# 4. Scan staged index diff (catches partial git add staging even if working tree was edited after)
STAGED_ADDED="$(git diff --cached -U0 | grep -E '^\+[^+]' || true)"
if [[ -n "${STAGED_ADDED}" ]]; then
    STAGED_LEAKS="$(printf '%s\n' "${STAGED_ADDED}" | grep -E "${LEAK_CONTENT_REGEX}" | head -n 20 || true)"
    if [[ -n "${EXTRA_CASE_INSENSITIVE_REGEX}" ]]; then
        STAGED_EXTRA="$(printf '%s\n' "${STAGED_ADDED}" | grep -iE "${EXTRA_CASE_INSENSITIVE_REGEX}" | head -n 20 || true)"
        [[ -n "${STAGED_EXTRA}" ]] && STAGED_LEAKS="${STAGED_LEAKS:+${STAGED_LEAKS}$'\n'}${STAGED_EXTRA}"
    fi
    if [[ -n "${STAGED_LEAKS}" ]]; then
        echo "ERROR [Sensitive Content Found in Staged Index]:" >&2
        echo "${STAGED_LEAKS}" >&2
        VIOLATIONS=$((VIOLATIONS + 1))
    fi
fi

# 5. Scan active branch Git commit history (HEAD) if commits exist
if git rev-parse --verify HEAD >/dev/null 2>&1; then
    ALL_HIST_PATHS="$(git log HEAD --name-only --pretty=format: | sort -u)"
    HIST_PATHS="$(check_forbidden_paths "${ALL_HIST_PATHS}")"
    if [[ -n "${HIST_PATHS}" ]]; then
        echo "ERROR [Forbidden Paths Found in Git Commit History]:" >&2
        echo "${HIST_PATHS}" >&2
        VIOLATIONS=$((VIOLATIONS + 1))
    fi

    HIST_ADDED_REMOVED="$(git log HEAD -p | grep -E '^[+-]' || true)"
    HIST_PATCH_LEAKS="$(printf '%s\n' "${HIST_ADDED_REMOVED}" | grep -E "${LEAK_CONTENT_REGEX}" | head -n 20 || true)"
    if [[ -n "${EXTRA_CASE_INSENSITIVE_REGEX}" ]]; then
        HIST_EXTRA="$(printf '%s\n' "${HIST_ADDED_REMOVED}" | grep -iE "${EXTRA_CASE_INSENSITIVE_REGEX}" | head -n 20 || true)"
        [[ -n "${HIST_EXTRA}" ]] && HIST_PATCH_LEAKS="${HIST_PATCH_LEAKS:+${HIST_PATCH_LEAKS}$'\n'}${HIST_EXTRA}"
    fi
    if [[ -n "${HIST_PATCH_LEAKS}" ]]; then
        echo "ERROR [Sensitive Content Found in Git Commit History]:" >&2
        echo "${HIST_PATCH_LEAKS}" >&2
        VIOLATIONS=$((VIOLATIONS + 1))
    fi

    AUTHOR_REGEX='(@[g]mail\.com|@[o]utlook\.|@[h]otmail\.|@[y]ahoo\.|@[o]range\.fr|@[w]anadoo\.fr|@[f]ree\.fr|@[d]ummy\.com)'
    AUTHOR_LIST="$(git log HEAD --format='%an <%ae>' | sort -u)"
    AUTHOR_LEAKS="$(printf '%s\n' "${AUTHOR_LIST}" | grep -Ei "${AUTHOR_REGEX}" || true)"
    if [[ -n "${EXTRA_CASE_INSENSITIVE_REGEX}" ]]; then
        # Scope instance-private author checks to commits ahead of upstream base when present,
        # so already-merged GitHub squash-merge commits on origin/main do not block new PRs.
        UPSTREAM_BASE=""
        for candidate_ref in \
            refs/remotes/origin/main \
            refs/remotes/origin/master \
            refs/remotes/upstream/main \
            refs/remotes/upstream/master; do
            if git rev-parse --verify --quiet "${candidate_ref}" >/dev/null 2>&1; then
                UPSTREAM_BASE="${candidate_ref}"
                break
            fi
        done
        if [[ -n "${UPSTREAM_BASE}" ]]; then
            PRIVATE_AUTHOR_LIST="$(git log "${UPSTREAM_BASE}..HEAD" --format='%an <%ae>' | sort -u)"
        else
            PRIVATE_AUTHOR_LIST="${AUTHOR_LIST}"
        fi
        if [[ -n "${PRIVATE_AUTHOR_LIST}" ]]; then
            AUTHOR_EXTRA="$(printf '%s\n' "${PRIVATE_AUTHOR_LIST}" | grep -Ei "${EXTRA_CASE_INSENSITIVE_REGEX}" || true)"
            [[ -n "${AUTHOR_EXTRA}" ]] && AUTHOR_LEAKS="${AUTHOR_LEAKS:+${AUTHOR_LEAKS}$'\n'}${AUTHOR_EXTRA}"
        fi
    fi
    if [[ -n "${AUTHOR_LEAKS}" ]]; then
        echo "ERROR [Personal Identity Found in Git Commit Metadata]:" >&2
        echo "${AUTHOR_LEAKS}" >&2
        VIOLATIONS=$((VIOLATIONS + 1))
    fi
fi

if [[ "${VIOLATIONS}" -gt 0 ]]; then
    echo "Upstream leak check FAILED (${VIOLATIONS} violation group(s) detected)." >&2
    exit 1
fi

echo "Zero Upstream Leak Guard passed (working tree, index & full git history clean)."
exit 0
