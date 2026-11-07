FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    TRANSFERV_DATA_DIR=/data

WORKDIR /app

# tini reaps orphaned ssh/sshpass processes (ControlPersist masters detach
# from their parent); without an init they pile up as zombies under PID 1.
RUN apt-get update \
    && apt-get install -y --no-install-recommends openssh-client rsync sshpass tini \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY app ./app

EXPOSE 8080

HEALTHCHECK --interval=60s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/api/health', timeout=4).status == 200 else 1)"

ENTRYPOINT ["/usr/bin/tini", "--"]
# One worker is enough (single background loop + SQLite). No access log:
# polling/SSE would otherwise write a line every few seconds forever.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080", \
     "--no-access-log", "--timeout-graceful-shutdown", "3"]
