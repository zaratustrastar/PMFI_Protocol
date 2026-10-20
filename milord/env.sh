# Sandbox helper for the build container this repo was developed in: use its pre-downloaded solc and foundry
# binaries when they exist. A no-op everywhere else (CI, a VPS, a laptop).
if [ -d /tmp/claude-0/foundry ]; then export PATH=/tmp/claude-0/foundry:$PATH; fi
if [ -x /root/.svm/0.8.26/solc-0.8.26 ]; then
  export FOUNDRY_SOLC=/root/.svm/0.8.26/solc-0.8.26
  export FOUNDRY_OFFLINE=true
fi
