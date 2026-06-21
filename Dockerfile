# syntax=docker/dockerfile:1

FROM node:22-bookworm-slim AS web-build
WORKDIR /build/web
COPY web/package.json web/package-lock.json ./
RUN npm ci
COPY web ./
RUN npm run build

FROM python:3.12-slim-bookworm AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    IRISCOPE_PROJECT_ROOT=/app \
    IRISCOPE_CAPTURES_ROOT=/data/captures \
    IRISCOPE_WEB_DIST=/app/web/dist \
    IRISCOPE_DOCKER=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
      ca-certificates \
      curl \
      ffmpeg \
      git \
      libgl1 \
      libglib2.0-0 \
      libgomp1 \
      openssh-client \
      rsync \
      v4l-utils \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml README.md ./
COPY iriscope ./iriscope
RUN python -m pip install --no-cache-dir --upgrade pip \
    && python -m pip install --no-cache-dir -e ".[web,webrtc,ssh]"

COPY --from=web-build /build/web/dist ./web/dist
COPY docker/iriscope-host-entrypoint.sh /usr/local/bin/iriscope-host-entrypoint
RUN chmod 0755 /usr/local/bin/iriscope-host-entrypoint
RUN mkdir -p /data/captures

EXPOSE 8765
ENTRYPOINT ["iriscope-host-entrypoint"]
CMD ["python", "-m", "uvicorn", "iriscope.web_api:app", "--host", "0.0.0.0", "--port", "8765"]
