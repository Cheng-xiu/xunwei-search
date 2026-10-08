FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    XUNWEI_HOST=0.0.0.0 XUNWEI_DATA_DIR=/data

WORKDIR /app
COPY run.py ./
COPY search_app ./search_app
COPY web ./web

RUN groupadd --gid 10001 xunwei \
    && useradd --uid 10001 --gid 10001 --create-home xunwei \
    && mkdir -p /data && chown xunwei:xunwei /data
USER 10001:10001

EXPOSE 8877
VOLUME ["/data"]
CMD ["python", "run.py", "--no-browser"]
