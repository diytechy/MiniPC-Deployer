#!/bin/bash
# Stage one validated private gateway without allowing this optional component
# to block the hub's core compose startup.
set -u

python_bin="$1"
installer="$2"
site_stamp="$3"
destination="$4"
shift 4

disable_gateway() {
    rm -rf -- "$destination"
}

if [ "$#" -ne 1 ]; then
    disable_gateway
    echo "WARNING: ambiguous private gateway payload ($# candidates); private access disabled and core startup continues" >&2
    exit 0
fi

scratch="$(mktemp -d)" || {
    disable_gateway
    echo "WARNING: cannot create private gateway staging directory; private access disabled and core startup continues" >&2
    exit 0
}
trap 'rm -rf -- "$scratch"' EXIT

if ! "$python_bin" "$installer" "$1" "$site_stamp" "$scratch/app"; then
    disable_gateway
    echo "WARNING: private gateway validation/install failed; private access disabled and core startup continues" >&2
    exit 0
fi

mkdir -p "$(dirname "$destination")"
disable_gateway
if ! mv -- "$scratch/app" "$destination"; then
    disable_gateway
    echo "WARNING: private gateway publication failed; private access disabled and core startup continues" >&2
    exit 0
fi

echo "PASS private gateway staged; enabling access remains an explicit operator action"
