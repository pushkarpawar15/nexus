FROM python:3.12-slim

# The approval and publication gates re-verify the execution patch by shelling
# out to Git against the downloaded workspace, so the API image needs Git and
# CA certificates just like the worker image.
RUN apt-get update \
    && apt-get install --no-install-recommends -y ca-certificates git \
    && rm -rf /var/lib/apt/lists/*

# Same Antigravity embedded-ripgrep loader shim as Dockerfile.worker, needed
# if this image ever runs an in-process Antigravity validation.
RUN mkdir -p /usr/grte/v5/lib64 \
    && ln -s /lib64/ld-linux-x86-64.so.2 /usr/grte/v5/lib64/ld-linux-x86-64.so.2

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY pyproject.toml ./pyproject.toml
COPY app ./app
RUN python -m pip install --no-cache-dir .

EXPOSE 8080
CMD ["sh", "-c", "uvicorn app.main:create_app --factory --host 0.0.0.0 --port ${PORT:-8080}"]
