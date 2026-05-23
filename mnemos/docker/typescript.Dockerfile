# TypeScript GitHub App image.
#
# Multi-stage:
#   1. deps installs production + dev deps from a cached layer.
#   2. builder runs `tsc` to emit dist/.
#   3. runtime keeps only the compiled output and production node_modules.
#
# One process hosts both the Probot webhook server and the hono callback
# server; they bind different ports inside the same container.

# ---------- deps ------------------------------------------------------------
FROM node:20-bookworm-slim AS deps

ENV NODE_ENV=development
WORKDIR /app

# Copy only manifests first so npm ci can cache across source edits.
COPY typescript/package.json typescript/package-lock.json* ./
RUN if [ -f package-lock.json ]; then npm ci; else npm install; fi


# ---------- builder ---------------------------------------------------------
FROM node:20-bookworm-slim AS builder

WORKDIR /app
COPY --from=deps /app/node_modules ./node_modules
COPY typescript/ ./
RUN npx tsc -p tsconfig.json


# ---------- prod-deps -------------------------------------------------------
# Install only runtime dependencies so the final image has no dev tooling.
FROM node:20-bookworm-slim AS prod-deps

WORKDIR /app
COPY typescript/package.json typescript/package-lock.json* ./
RUN if [ -f package-lock.json ]; then \
      npm ci --omit=dev; \
    else \
      npm install --omit=dev; \
    fi


# ---------- runtime ---------------------------------------------------------
FROM node:20-bookworm-slim AS runtime

ENV NODE_ENV=production \
    PORT=3000 \
    CALLBACK_PORT=3001

RUN apt-get update \
 && apt-get install --no-install-recommends -y curl \
 && rm -rf /var/lib/apt/lists/* \
 && groupadd --system --gid 10001 mnemos \
 && useradd --system --gid mnemos --uid 10001 --home /app mnemos

WORKDIR /app
COPY --from=prod-deps --chown=mnemos:mnemos /app/node_modules ./node_modules
COPY --from=builder   --chown=mnemos:mnemos /app/dist         ./dist
COPY --from=builder   --chown=mnemos:mnemos /app/package.json ./package.json

USER mnemos

EXPOSE 3000 3001

HEALTHCHECK --interval=15s --timeout=3s --start-period=20s --retries=3 \
  CMD curl -fsS "http://127.0.0.1:${CALLBACK_PORT}/healthz" || exit 1

CMD ["node", "dist/app.js"]
