# syntax=docker/dockerfile:1.27.1@sha256:4edf897a3ffa55b89f906fc8cc78afdb3f1834cc9c7083565e611a8a7d5fe99e
# The e2e stack's stand-in for Google's token and JWKS hosts. Not part of the release image.
FROM node:24.22.3-bookworm-slim@sha256:dc26ae92c9fb15cf6b1d63e20f0944b2efe0f115b20e00be48fb23fceb061c54

WORKDIR /app
COPY --chown=root:root server.mjs ./
USER node
EXPOSE 443 9100
CMD ["node", "server.mjs"]
