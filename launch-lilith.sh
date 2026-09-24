#!/usr/bin/env bash
set -euo pipefail

project_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
python="$project_dir/.venv/bin/python"

if [[ ! -x "$python" ]]; then
    printf 'Set up Lilith first:\n\n' >&2
    printf '  cd %q\n' "$project_dir" >&2
    printf '  python3.12 -m venv .venv\n' >&2
    printf '  .venv/bin/python -m pip install -e .\n\n' >&2
    exit 1
fi

# Owner-authorized shell access for generated tools. Set to 0 to revoke it.
export LILITH_ALLOW_SHELL="${LILITH_ALLOW_SHELL:-1}"
exec "$python" -m lilith.home "$@"
