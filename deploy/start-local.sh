set -eu
project_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
exec bash "$project_root/deploy/start-admin.sh" "$@"
