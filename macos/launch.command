#!/bin/bash
#
# Runs inside a Terminal window opened by LanChat.app. Starts the chat.
# It lives inside the app bundle next to the bundled `lanchat` package.

cd "$(dirname "$0")" || exit 1

if ! command -v python3 >/dev/null 2>&1; then
	echo
	echo "Python 3 was not found."
	echo "Install the Xcode Command Line Tools with:"
	echo
	echo "    xcode-select --install"
	echo
	read -n 1 -s -r -p "Press any key to close..."
	echo
	exit 1
fi

export PYTHONPATH="$(pwd)"
exec python3 -m lanchat --nick "${USER:-friend}"
