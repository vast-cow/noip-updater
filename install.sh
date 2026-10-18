#!/bin/sh
set -eu
exec python3 "$(dirname "$0")/manage_install.py" install "$@"
