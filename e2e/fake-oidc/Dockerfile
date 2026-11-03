# syntax=docker/dockerfile:1.27.1@sha256:4edf897a3ffa55b89f906fc8cc78afdb3f1834cc9c7083565e611a8a7d5fe99e
# The e2e stack's stand-in for Google's token and JWKS hosts. Not part of the release image.
FROM node:24.22.1-bookworm-slim@sha256:be8204d4bd327363357f532187b752ff62759c31bedf98e9e18fc08c9cbd605c

WORKDIR /app
COPY --chown=root:root server.mjs ./
USER node
EXPOSE 443 9100
CMD ["node", "server.mjs"]
