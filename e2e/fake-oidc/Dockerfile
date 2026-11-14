# syntax=docker/dockerfile:1.27.1@sha256:4edf897a3ffa55b89f906fc8cc78afdb3f1834cc9c7083565e611a8a7d5fe99e
# The e2e stack's stand-in for Google's token and JWKS hosts. Not part of the release image.
FROM node:24.22.2-bookworm-slim@sha256:01d566e8d94d8c619591d78652babfccd3c6992a3dffa9ed5ab39e11cf6c70e9

WORKDIR /app
COPY --chown=root:root server.mjs ./
USER node
EXPOSE 443 9100
CMD ["node", "server.mjs"]
