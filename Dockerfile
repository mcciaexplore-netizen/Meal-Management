# One image for both applications. Choose the process at runtime with
# MEAL_APPLICATION=admin (default) or MEAL_APPLICATION=scanner.

# Base images come from the ECR Public mirror of Docker Official Images to avoid
# Docker Hub pull limits in CodeBuild.
FROM public.ecr.aws/docker/library/node:20-slim AS frontend
WORKDIR /build/frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY frontend/ ./
RUN npm run build


FROM public.ecr.aws/docker/library/python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONPATH=/app/backend \
    MEAL_APPLICATION=admin \
    PORT=8000

WORKDIR /app

# Install only the dependencies declared in pyproject.toml (plus the AWS extra).
# The package itself runs from /app/backend so it can locate frontend/dist and
# database/migrations relative to the project root.
COPY pyproject.toml ./
RUN python -c "import tomllib; p = tomllib.load(open('pyproject.toml', 'rb'))['project']; print('\n'.join(p['dependencies'] + p['optional-dependencies']['production']))" > /tmp/requirements.txt \
    && pip install -r /tmp/requirements.txt \
    && rm /tmp/requirements.txt

COPY backend/ backend/
COPY database/ database/
COPY manage.py ./
COPY deploy/docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
COPY --from=frontend /build/frontend/dist frontend/dist

RUN groupadd --system meal && useradd --system --gid meal --home-dir /app meal \
    && mkdir -p /app/var/private && chown -R meal:meal /app/var \
    && chmod 0755 /usr/local/bin/docker-entrypoint.sh

USER meal
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import os, urllib.request as u; h = os.environ.get('HEALTHCHECK_HOST', 'localhost'); r = u.Request('http://127.0.0.1:' + os.environ.get('PORT', '8000') + '/health/live', headers={'Host': h, 'X-Forwarded-Proto': 'https'}); u.urlopen(r, timeout=4)" || exit 1

ENTRYPOINT ["docker-entrypoint.sh"]
