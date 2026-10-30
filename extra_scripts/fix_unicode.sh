#!/usr/bin/env bash
#
# fix_unicode.sh - replace AI-writing typographic Unicode with plain ASCII,
# and (optionally) lint AI-writing prose patterns from DOC_STYLE.md.
#
# Modes:
#   Fix mode  (default)   Rewrite Unicode in-place. Never rewrites prose.
#   Warn mode (--warn)    Read-only lint. Exit 1 on issues.
#   Danger-fix (--danger-fix)
#                         Unicode + stem-aware lexical rewrites from
#                         style_rules.json. Implies --style.
#   --staged / --changed  Warn-only against git-tracked changed files.
#   --pr                  Warn-only on *.py vs base ref.
#
# Usage:
#   extra_scripts/fix_unicode.sh [OPTIONS] [<file|dir> ...]
#
# Options:
#   --warn, -w             Warn-only; list offenders, do not modify
#   --staged               Warn against staged files
#   --changed              Warn against modified/new files vs HEAD
#   --pr                   Warn against *.py vs base ref (origin/dev...HEAD)
#   --pr --fix             Fix those *.py files instead of warning
#   --base-ref REF         Override base ref for --pr (default: origin/dev -> origin/main)
#   --strip-emoji          Also strip / warn about emoji
#   --recursive, -r        Recurse into directories (implied for default paths)
#   --ext LIST             Comma-separated extensions
#                          (default: py,md,txt,json,yaml,yml,rst,toml)
#   --dry-run, -n          Show diff, do not modify (fix mode only)
#   --style                Enable DOC_STYLE.md style checks (warn output)
#   --style-only           Style checks only, skip Unicode
#   --style-strict         Treat all style hits as errors
#   --danger-fix           Rewrite stem-matched lexical hits. Implies --style.
#                          Plain --fix never touches them.
#   --rules-file PATH      Override rules JSON (default: style_rules.json)
#   --help, -h             Show this help
#
# Default paths (when no paths are given, fix mode):
#   docs/**/*.md
#
# Exit codes:
#   0   Clean / fixes applied
#   1   Warn mode: at least one issue
#   2   Usage / missing dependency / bad rules file
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
WORKER="${SCRIPT_DIR}/_fix_unicode_worker.py"

# --- Argument parsing ---------------------------------------------------------

MODE="fix"               # fix | warn | danger-fix
GIT_SCOPE=""
BASE_REF_OVERRIDE=""
STRIP_EMOJI=0
RECURSIVE=1
DRY_RUN=0
STYLE=0
STYLE_ONLY=0
STYLE_STRICT=0
RULES_FILE=""
EXT_LIST="py,md,txt,json,yaml,yml,rst,toml"
declare -a TARGETS=()

print_help() {
  sed -n '2,46p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --warn|-w)        MODE="warn"; shift ;;
    --fix)            MODE="fix"; shift ;;
    --danger-fix)     MODE="danger-fix"; STYLE=1; shift ;;
    --staged)         MODE="warn"; GIT_SCOPE="staged"; shift ;;
    --changed)        MODE="warn"; GIT_SCOPE="changed"; shift ;;
    --pr)             MODE="warn"; GIT_SCOPE="pr"; shift ;;
    --base-ref)       BASE_REF_OVERRIDE="${2:-}"; shift 2 ;;
    --strip-emoji)    STRIP_EMOJI=1; shift ;;
    --recursive|-r)   RECURSIVE=1; shift ;;
    --dry-run|-n)     DRY_RUN=1; shift ;;
    --ext)            EXT_LIST="${2:-}"; shift 2 ;;
    --style)          STYLE=1; shift ;;
    --style-only)     STYLE=1; STYLE_ONLY=1; shift ;;
    --style-strict)   STYLE=1; STYLE_STRICT=1; shift ;;
    --rules-file)     RULES_FILE="${2:-}"; shift 2 ;;
    --help|-h)        print_help; exit 0 ;;
    -*)               echo "Unknown option: $1" >&2; print_help >&2; exit 2 ;;
    *)                TARGETS+=("$1"); shift ;;
  esac
done

command -v python3 >/dev/null 2>&1 || { echo "python3 not found" >&2; exit 2; }
[[ -f "${WORKER}" ]] || { echo "worker not found: ${WORKER}" >&2; exit 2; }

# Build worker flag array (passed after the mode and path).
declare -a WORKER_FLAGS=()
[[ "${STRIP_EMOJI}" -eq 1 ]] && WORKER_FLAGS+=(--strip-emoji)
[[ "${STYLE}" -eq 1 ]]        && WORKER_FLAGS+=(--style)
[[ "${STYLE_ONLY}" -eq 1 ]]   && WORKER_FLAGS+=(--style-only)
[[ "${STYLE_STRICT}" -eq 1 ]] && WORKER_FLAGS+=(--style-strict)
[[ -n "${RULES_FILE}" ]]      && WORKER_FLAGS+=(--rules-file "${RULES_FILE}")

# --- Build extension pattern for find ----------------------------------------

IFS=',' read -ra _EXTS <<< "${EXT_LIST}"
EXT_FIND_ARGS=()
for ext in "${_EXTS[@]}"; do
  ext="$(echo "${ext}" | tr -d ' ')"
  [[ -n "${ext}" ]] || continue
  if [[ ${#EXT_FIND_ARGS[@]} -gt 0 ]]; then
    EXT_FIND_ARGS+=(-o)
  fi
  EXT_FIND_ARGS+=(-name "*.${ext}")
done

# --- Collect files ------------------------------------------------------------

declare -a FILES=()

if [[ -n "${GIT_SCOPE}" ]]; then
  cd "${REPO_ROOT}"
  if [[ "${GIT_SCOPE}" == "staged" ]]; then
    mapfile -t _git_files < <(git diff --cached --name-only 2>/dev/null || true)
  elif [[ "${GIT_SCOPE}" == "pr" ]]; then
    if [[ -n "${BASE_REF_OVERRIDE}" ]]; then
      _base="${BASE_REF_OVERRIDE}"
    elif git rev-parse --verify origin/dev &>/dev/null; then
      _base="origin/dev"
    else
      _base="origin/main"
    fi
    echo "fix_unicode --pr: checking *.py vs ${_base}..."
    mapfile -t _git_files < <(git diff --name-only "${_base}...HEAD" 2>/dev/null \
      | grep -E '\.py$' || true)
  else
    mapfile -t _git_files < <(git diff --name-only HEAD 2>/dev/null || true)
    mapfile -t _git_new   < <(git ls-files --others --exclude-standard 2>/dev/null || true)
    _git_files+=("${_git_new[@]}")
  fi
  for f in "${_git_files[@]}"; do
    [[ -z "${f}" ]] && continue
    abs="${REPO_ROOT}/${f}"
    [[ -f "${abs}" ]] || continue
    fname="$(basename "${f}")"
    [[ "${fname}" == *.sh ]] && continue
    matched=0
    for ext in "${_EXTS[@]}"; do
      ext="$(echo "${ext}" | tr -d ' ')"
      [[ "${fname}" == *.${ext} ]] && matched=1 && break
    done
    [[ "${matched}" -eq 1 ]] && FILES+=("${abs}")
  done

elif [[ ${#TARGETS[@]} -eq 0 ]]; then
  cd "${REPO_ROOT}"
  mapfile -t _docs   < <(find "docs" -type f -name "*.md" 2>/dev/null | sort)
  for f in "${_docs[@]}"; do
    [[ -f "${f}" ]] && FILES+=("${REPO_ROOT}/${f#./}")
  done

else
  for target in "${TARGETS[@]}"; do
    if [[ -f "${target}" ]]; then
      FILES+=("${target}")
    elif [[ -d "${target}" ]]; then
      if [[ "${RECURSIVE}" -eq 1 ]]; then
        mapfile -t _found < <(find "${target}" -type f \( "${EXT_FIND_ARGS[@]}" \) 2>/dev/null | sort)
      else
        mapfile -t _found < <(find "${target}" -maxdepth 1 -type f \( "${EXT_FIND_ARGS[@]}" \) 2>/dev/null | sort)
      fi
      FILES+=("${_found[@]}")
    else
      echo "WARNING: not a file or directory: ${target}" >&2
    fi
  done
fi

if [[ ${#FILES[@]} -eq 0 ]]; then
  echo "No matching files found." >&2
  exit 0
fi

# --- Process files ------------------------------------------------------------

WARN_COUNT=0
MODIFIED_COUNT=0
UNCHANGED_COUNT=0
FATAL=0

run_worker() {
  # $1 = mode, $2 = file path
  if [[ ${#WORKER_FLAGS[@]} -gt 0 ]]; then
    python3 "${WORKER}" "$1" "$2" "${WORKER_FLAGS[@]}"
  else
    python3 "${WORKER}" "$1" "$2"
  fi
}

for f in "${FILES[@]}"; do
  if [[ "${MODE}" == "fix" || "${MODE}" == "danger-fix" ]]; then
    if [[ "${DRY_RUN}" -eq 1 ]]; then
      tmp="$(mktemp)"
      cp "${f}" "${tmp}"
      run_worker "${MODE}" "${tmp}" > /dev/null 2>&1 || true
      if ! diff -q "${f}" "${tmp}" > /dev/null 2>&1; then
        echo "--- ${f}"
        diff -u "${f}" "${tmp}" || true
        MODIFIED_COUNT=$((MODIFIED_COUNT + 1))
      else
        UNCHANGED_COUNT=$((UNCHANGED_COUNT + 1))
      fi
      rm -f "${tmp}"
    else
      set +e
      output="$(run_worker "${MODE}" "${f}")"
      rc=$?
      set -e
      if [[ "${rc}" -eq 2 ]]; then
        echo "${output}" >&2
        FATAL=1
        break
      fi
      echo "${output}"
      while IFS= read -r line; do
        case "${line}" in
          MODIFIED*)  MODIFIED_COUNT=$((MODIFIED_COUNT + 1)) ;;
          UNCHANGED*) UNCHANGED_COUNT=$((UNCHANGED_COUNT + 1)) ;;
        esac
      done <<< "${output}"
    fi
  else
    set +e
    output="$(run_worker warn "${f}" 2>&1)"
    rc=$?
    set -e
    if [[ "${rc}" -eq 2 ]]; then
      echo "${output}" >&2
      FATAL=1
      break
    fi
    if [[ -n "${output}" ]]; then
      echo "${output}"
    fi
    if [[ "${rc}" -ne 0 ]]; then
      WARN_COUNT=$((WARN_COUNT + $(echo "${output}" | grep -c "^WARNING")))
    fi
  fi
done

if [[ "${FATAL}" -eq 1 ]]; then
  exit 2
fi

# --- Summary ------------------------------------------------------------------

echo
if [[ "${MODE}" == "warn" ]]; then
  if [[ "${WARN_COUNT}" -gt 0 ]]; then
    echo "Warnings: ${WARN_COUNT} issue(s) found."
    exit 1
  else
    echo "Clean - no issues found."
    exit 0
  fi
else
  if [[ "${DRY_RUN}" -eq 1 ]]; then
    echo "Dry-run: ${MODIFIED_COUNT} file(s) would be modified, ${UNCHANGED_COUNT} unchanged."
  else
    echo "Modified: ${MODIFIED_COUNT}  Unchanged: ${UNCHANGED_COUNT}"
  fi
fi
