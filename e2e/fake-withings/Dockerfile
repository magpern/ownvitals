# syntax=docker/dockerfile:1.27.1@sha256:4edf897a3ffa55b89f906fc8cc78afdb3f1834cc9c7083565e611a8a7d5fe99e
# The e2e stack's stand-in for Withings: an HTTP shell (server.py, standard library only) around
# the protocol code of backend/tests, which compose.e2e.yaml mounts read-only at /protocol.
# Not part of the release image.
FROM python:3.12.16-slim-bookworm@sha256:e697c81897302a4c4333bade000d98038fbb21eb064f8ee1fd42d02ebcd9d337

RUN useradd --system --uid 10002 --no-create-home fakewithings
WORKDIR /app
COPY --chown=root:root server.py ./
USER fakewithings
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
EXPOSE 443 9101
CMD ["python", "server.py"]
