#!/bin/sh
# One-command Webserv tester launcher. Run from any directory.
set -eu
HERE=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
if [ "${1:-}" != "" ]; then
    case "$1" in
        -*) exec python3 "$HERE/webserv_tester.py" --auto "$@" ;;
        *) BIN="$1"; shift; exec python3 "$HERE/webserv_tester.py" --auto --bin "$BIN" "$@" ;;
    esac
fi
exec python3 "$HERE/webserv_tester.py" --auto
