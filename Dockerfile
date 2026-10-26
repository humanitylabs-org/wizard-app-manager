ARG NODE_VERSION="jod"
ARG ALPINE_VERSION="3.21"
ARG BUN_VERSION="1.3.0"

# The JS build (bun install, frontend, backend bundle) is the same bytes on every architecture, so it always runs
# on the build machine. Native arm64 bun hangs in "Resolving dependencies" (three bounded attempts, all stuck).
# Only the small runner stage (node + argon2 prebuilds + docker-compose for TARGETARCH) is built per target.
FROM --platform=$BUILDPLATFORM oven/bun:${BUN_VERSION}-alpine AS bun_base
FROM node:${NODE_VERSION}-alpine${ALPINE_VERSION} AS node_base

# ---- BUILDER BASE ----
FROM bun_base AS builder_base

WORKDIR /deps

ARG TARGETARCH
ARG DOCKER_COMPOSE_VERSION="v2.40.0"
ENV TARGETARCH=${TARGETARCH}

RUN apk add --no-cache curl python3 make g++ git

RUN echo "Building for ${TARGETARCH}"
RUN if [ "${TARGETARCH}" = "arm64" ]; then \
      curl -L -o docker-binary "https://github.com/docker/compose/releases/download/$DOCKER_COMPOSE_VERSION/docker-compose-linux-aarch64"; \
      elif [ "${TARGETARCH}" = "amd64" ]; then \
      curl -L -o docker-binary "https://github.com/docker/compose/releases/download/$DOCKER_COMPOSE_VERSION/docker-compose-linux-x86_64"; \
      fi

RUN chmod +x docker-binary

# ---- RUNNER BASE ----
FROM node_base AS runner_base

# WizardStorage drives copy-once migrations and volume aliases through the docker CLI.
RUN apk add --no-cache curl openssl git docker-cli && docker --version

# ---- BUILDER ----
FROM builder_base AS builder

ARG TIPI_VERSION
ARG LOCAL

ENV SENTRY_RELEASE=${TIPI_VERSION}

WORKDIR /app

COPY ./bun.lock ./
COPY ./package.json ./
COPY ./packages/backend/package.json ./packages/backend/package.json
COPY ./packages/frontend/package.json ./packages/frontend/package.json
COPY ./packages/common/package.json ./packages/common/package.json
COPY ./packages/frontend/scripts ./packages/frontend/scripts
COPY ./packages/frontend/public ./packages/frontend/public

# Bounded independently of the CI job, so a stall fails fast with a readable log.
RUN timeout 600 bun install --frozen-lockfile

COPY ./turbo.json ./turbo.json
COPY ./packages ./packages
COPY ./wam/hermes-lock.json ./wam/hermes-lock.json
RUN bun run build

RUN echo "TIPI_VERSION: ${SENTRY_RELEASE}"
RUN echo "LOCAL: ${LOCAL}"

RUN bun run bundle
RUN --mount=type=secret,id=sentry_token,env=SENTRY_AUTH_TOKEN if [ "${LOCAL}" != "true" ]; then \
  cd ./packages/backend && \
  bun run sentry:sourcemaps; \
  fi

# ---- RUNNER ----
FROM runner_base AS runner

ENV NODE_ENV="production"

WORKDIR /app

RUN npm install --no-save --omit=dev argon2 class-transformer

COPY --from=builder_base /deps/docker-binary /usr/local/bin/docker-compose
COPY --from=builder /app/package.json ./
COPY --from=builder /app/packages/backend/dist ./

# Assets
COPY --from=builder /app/packages/backend/assets ./assets
COPY --from=builder /app/packages/backend/src/core/database/drizzle ./assets/migrations
COPY --from=builder /app/packages/backend/src/modules/i18n/translations ./assets/translations
COPY --from=builder /app/packages/frontend/dist/client ./assets/frontend

EXPOSE 3000

RUN mv main.js main.mjs

CMD ["node", "./main.mjs"]
