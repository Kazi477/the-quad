FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends nginx ffmpeg gettext-base ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY the_network_production.py /app/app.py
COPY nginx.conf.template /app/nginx.conf.template
COPY start.sh /app/start.sh
RUN chmod 0755 /app/start.sh \
    && mkdir -p /var/data

ENV PYTHONUNBUFFERED=1 \
    PRODUCTION=1 \
    TRUST_PROXY=1 \
    BACKEND_PORT=8081 \
    DATA_DIR=/var/data

CMD ["/app/start.sh"]
