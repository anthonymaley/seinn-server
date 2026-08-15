# seinn-agent container — third deployment shape beside systemd and launchd.
# The agent itself stays stdlib-only; this image adds exactly ffmpeg (thumbs,
# durations, seinn-convert) and gosu (PUID/PGID privilege drop — Debian's
# packaged standard for the entrypoint-remap pattern; exec-chains so signals
# reach Python directly).
FROM python:3.12-slim

ARG AGENT_VERSION=dev
LABEL org.opencontainers.image.title="seinn-agent" \
      org.opencontainers.image.description="seinn media agent: listings, range streaming, progress, thumbnails, delete" \
      org.opencontainers.image.version="${AGENT_VERSION}" \
      org.opencontainers.image.vendor="seinn" \
      org.opencontainers.image.licenses="UNLICENSED"
# No org.opencontainers.image.source: the repo is private and the public-
# release decision is pending — do not invent a URL.

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg gosu \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd -g 1000 seinn \
    && useradd -u 1000 -g seinn -M -s /usr/sbin/nologin seinn \
    && mkdir -p /config /media \
    && chown seinn:seinn /config

COPY seinn_agent.py seinn_convert.py seinn_web.html /app/
# Weefish-only selftest — inert without a mounted libkrutho.so and the SDK
# fixture corpus; shipped so the image is the complete server-component view.
COPY tools/krutho_selftest.py /app/tools/
COPY docker-entrypoint.sh /docker-entrypoint.sh
RUN chmod 0755 /docker-entrypoint.sh

EXPOSE 8378

# /api/roots is an open read (no token needed) — the natural liveness probe.
# Assumes the in-container default port 8378; remap outside with -p.
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD ["python3", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8378/api/roots', timeout=3).status == 200 else 1)"]

ENTRYPOINT ["/docker-entrypoint.sh"]
