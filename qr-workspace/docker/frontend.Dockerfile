FROM node:24-bookworm-slim@sha256:c86ecead9e55a5210ac1727de38a4ca840bf03dcaf34dc9749addbf383b1920c
RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates && rm -rf /var/lib/apt/lists/* \
    && corepack enable
WORKDIR /workspace/frequi
COPY frequi/package.json /workspace/frequi/package.json
RUN corepack install && pnpm config set store-dir /pnpm/store
