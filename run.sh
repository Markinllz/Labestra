#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
if [[ ! -x .venv/bin/python ]]; then
  interpreter="${PYTHON:-}"
  if [[ -z "$interpreter" ]]; then
    for candidate in python3.13 python3.12 python3.11 python3; do
      if command -v "$candidate" >/dev/null 2>&1 && "$candidate" -c 'import sys; sys.exit(sys.version_info < (3,11))' 2>/dev/null; then
        interpreter="$candidate"
        break
      fi
    done
  fi
  if [[ -z "$interpreter" ]]; then
    echo 'Установите Python 3.11+ или укажите PYTHON=/путь/к/python3.11' >&2
    exit 2
  fi
  "$interpreter" -m venv .venv
fi
python=.venv/bin/python
command="${1:---help}"
if [[ $# -gt 0 ]]; then shift; fi
case "$command" in
  check)
    "$python" -m dataset_tools.validation
    exec "$python" -m unittest discover -s tests -v "$@" ;;
  configure|pack) exec "$python" -m labestra.launch "$command" "$@" ;;
  baseline|system) exec "$python" -m labestra.launch run --variant "$command" "$@" ;;
  fixture) exec "$python" -m labestra --fixture-actions "$@" ;;
  scenarios) exec "$python" -m labestra.scenarios "$@" ;;
  evaluate) exec "$python" -m labestra.evaluate "$@" ;;
  *) echo 'Команды: ./run.sh check | configure | baseline | system | fixture | scenarios | evaluate | pack'; ;;
esac
