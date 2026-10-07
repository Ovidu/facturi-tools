# Build context: the folder that holds this file and facturi_tools/.
# Pinned rclone: the official image installs the static binary at /usr/local/bin/rclone.
# Settings the container reads (names only, set them in the platform, never in the image):
#   FACTURI_KEY        shared secret clients send in X-Facturi-Key (24+ characters)
#   FACTURI_REMOTE     rclone destination folder all uploads go under
#   FACTURI_BUYER_CUI  buyer tax number used for the invoice VAT check
#   FACTURI_ADMIN      set to 1 to enable the config upload route
#   PORT               listening port (default 8080)
#   RCLONE_CONFIG      rclone config file path (default /config/rclone/rclone.conf)
FROM rclone/rclone:1.75.1 AS rclone

FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir pymupdf==1.28.2

COPY --from=rclone /usr/local/bin/rclone /usr/local/bin/rclone

# rclone rewrites its config when it refreshes the OneDrive token, so the user must own this folder
RUN useradd --system --uid 10001 --no-create-home --shell /usr/sbin/nologin facturi \
    && mkdir -p /config/rclone /app \
    && chown -R facturi:facturi /config/rclone

WORKDIR /app
COPY facturi_tools /app/facturi_tools

ENV RCLONE_CONFIG=/config/rclone/rclone.conf \
    PORT=8080 \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# Config volume: mount a named volume or a host folder at /config/rclone (the folder, never the single file).
# A host folder must be owned by uid 10001 (chown 10001:10001), or the service cannot write the config and
# rclone cannot save refreshed tokens. Without a mount, the config lives in an anonymous volume and is lost
# on redeploy. GET /health reports configPresent, configDirWritable and remoteSet.
VOLUME /config/rclone
USER facturi
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:${PORT}/health', timeout=3)"

CMD ["python", "-m", "facturi_tools.server"]
