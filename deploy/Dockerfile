# FURY container. Build from the repo root:  docker build -f deploy/Dockerfile -t fury .
# NOTE: written but not yet built or run by the author (no Docker on the dev machine). Verify before relying on it.
# Secrets are NOT in the image (see .dockerignore): pass them at run time (env file, or FURY_SECRETS_PROVIDER=aws).
FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
RUN useradd --create-home --uid 10001 fury && mkdir /data && chown fury:fury /data
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt waitress
COPY --chown=fury:fury . .
USER fury
# Listens on all interfaces INSIDE the container only; publish it to the proxy network, never to the internet.
ENV FURY_HOST=0.0.0.0 FURY_PORT=8080 FURY_DB=/data/fury.db FURY_AUDIT_LOG=/data/audit.log FURY_TRACE_DB=/data/traces.db
VOLUME /data
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/', timeout=4).status==200 else 1)"
CMD ["python", "run_fury.py"]
