#!/bin/sh
# Stand-in for solc used when MILORD_PREBUILT=1: answers forge's version probe and refuses any real
# compilation, so a run on prebuilt production artifacts can never fall back to a via-IR build.
if [ "$1" = "--version" ]; then
  echo "solc, the solidity compiler commandline interface"
  echo "Version: 0.8.26+commit.8a97fa7a.Linux.g++"
  exit 0
fi
cat >/dev/null
echo "refusing to compile: the prebuilt production artifacts in out/ and cache/ do not match this checkout" >&2
exit 1
