FROM alpine:3.20

ARG MEDIAMTX_VERSION=1.12.2
ARG TARGETARCH=amd64

# font-dejavu: the concat chain burns an index number into each video (drawtext)
RUN apk add --no-cache python3 ffmpeg font-dejavu ca-certificates curl \
 && curl -fL "https://github.com/bluenviron/mediamtx/releases/download/v${MEDIAMTX_VERSION}/mediamtx_v${MEDIAMTX_VERSION}_linux_${TARGETARCH}.tar.gz" \
    | tar -xz -C /usr/local/bin mediamtx \
 && apk del curl

# Baked defaults so docker-compose needs no environment block.
#   MODE=prod            loader animation + URL list (dev = full logs on stdout)
#   ADVERTISE_IP=""      blank = auto-detect the host LAN IP for the printed URLs
#   VIDEOS_DIR/WORKSPACE_DIR  pin the mounts to /app/* so the config can live in a
#                        separate mounted /app/config dir (see entrypoint.sh)
# (The open-file limit for 100+ clients is raised by streamer.py at startup,
#  so no compose `ulimits` is needed either.)
ENV MODE=prod \
    ADVERTISE_IP="" \
    VIDEOS_DIR=/app/video_samples \
    WORKSPACE_DIR=/app/workspace \
    PYTHONUNBUFFERED=1 \
    COLORTERM=truecolor

WORKDIR /app

# App code. The config ships as a *default* under /app/defaults — a bind-mounted
# /app/config can't shadow it, so the entrypoint can seed it on first run.
COPY streamer.py loader.py probe_feeder.py ptyrun.py entrypoint.sh /app/
COPY config.ini /app/defaults/config.ini

RUN chmod +x /app/entrypoint.sh \
 && mkdir -p /app/video_samples /app/workspace /app/config

# RTSP (tcp) — first free port from config.ini [streamer] ports is used.
# With transport=udp, RTP/RTCP listen on port+10000 / port+10001 (udp).
EXPOSE 41000 41001 41002

# Healthy = the bound RTSP port accepts connections. streamer.py writes that
# port to workspace/.port once mediamtx is up; until then (first-run
# transcodes can take a while) a live streamer.py process counts as healthy,
# so a long prepare phase never flaps the health status.
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
  CMD sh -c 'nc -z 127.0.0.1 "$(cat /app/workspace/.port 2>/dev/null || echo 0)" 2>/dev/null || pgrep -f streamer.py >/dev/null'

# OCI metadata — shows up on Docker Hub / `docker inspect`.
LABEL org.opencontainers.image.title="RTSP Streamer" \
      org.opencontainers.image.description="Serve every video in a folder as a looping live RTSP stream — for benchmarking video-analytics engines. MediaMTX + ffmpeg, probe mode, mini-clip mode, concat chains, phase-shifted camera simulation." \
      org.opencontainers.image.version="2.5.0" \
      org.opencontainers.image.licenses="MIT"

ENTRYPOINT ["/app/entrypoint.sh"]
