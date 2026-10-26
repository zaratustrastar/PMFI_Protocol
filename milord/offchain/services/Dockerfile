# MILORD automation services (keeper, epoch publisher, root checker, monitor, quote server). One image, five commands.
#   docker build -f offchain/services/Dockerfile -t milord-services .     (from the milord/ directory)
FROM node:22-alpine
WORKDIR /app
COPY package.json package-lock.json* ./
RUN npm ci --omit=dev --ignore-scripts || npm install --omit=dev --ignore-scripts
COPY offchain/lib ./offchain/lib
COPY offchain/services ./offchain/services
COPY offchain/quote-server.mjs ./offchain/quote-server.mjs
# /data must exist and belong to node before the volume is created from it, or state writes fail as EACCES
RUN mkdir -p /data && chown node:node /data
USER node
ENV STATE_DIR=/data NODE_ENV=production
VOLUME ["/data"]
HEALTHCHECK --interval=60s --timeout=5s --retries=3 CMD wget -qO- http://127.0.0.1:${HEALTH_PORT:-9100}/healthz || exit 1
CMD ["node", "offchain/services/keeper.mjs"]
