FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY pyproject.toml ./
COPY src ./src
RUN pip install ".[transcode]"

# Numeric UID keeps the image free of user-management tooling.
RUN mkdir -p /data/recordings && chown 10001:10001 /data/recordings
USER 10001:10001

EXPOSE 9100 8080

ENTRYPOINT ["python", "-m", "radio_record"]
CMD ["--port", "9100", "--dashboard-port", "8080", "--dashboard-host", "0.0.0.0", "--output", "/data/recordings"]
