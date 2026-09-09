set -eu
project_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
if [ ! -x "$project_root/.venv/bin/python" ]; then
    printf '%s\n' 'Project .venv Python is missing. Create the virtual environment and install the approved project requirements.' >&2
    exit 1
fi
export PYTHONPATH="$project_root/backend${PYTHONPATH:+:$PYTHONPATH}"
exec "$project_root/.venv/bin/python" -m meal_management.local_server admin "$@"
