#!/bin/bash
set -e
cd -- "$(dirname -- "$(readlink -f -- "$0")")"
exec /usr/bin/python3 -m toy_sync "$@"
