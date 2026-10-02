FROM python:3.13.12-slim-bookworm AS wheels
WORKDIR /build
COPY requirements.lock ./
RUN python -m pip download --only-binary=:all: -r requirements.lock -d /wheels

FROM python:3.13.12-slim-bookworm
ARG BUILD_VERSION=0.2.2
ARG BUILD_ARCH=amd64
LABEL io.hass.version=$BUILD_VERSION io.hass.type="app" io.hass.arch=$BUILD_ARCH
LABEL org.opencontainers.image.source="https://github.com/Yiivgeny/ha-control"
LABEL org.opencontainers.image.description="Five generic tools for Home Assistant administration"
LABEL org.opencontainers.image.licenses="MIT"
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.lock ./
COPY --from=wheels /wheels /wheels
RUN python -m pip install --no-cache-dir --no-index --find-links=/wheels -r requirements.lock && rm -rf /wheels
COPY ha_control ./ha_control
CMD ["python", "-m", "ha_control.server"]
