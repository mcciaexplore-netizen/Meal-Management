#!/bin/sh
set -eu

case "${MEAL_APPLICATION:-admin}" in
    admin) module=meal_management.admin_api ;;
    scanner) module=meal_management.scanner_api ;;
    *)
        printf '%s\n' 'MEAL_APPLICATION must be admin or scanner.' >&2
        exit 1
        ;;
esac

# Secret stores (ECS, App Runner) usually inject the database CA as text.
# Write it to a private file so DB_SSL_CA can point at it.
if [ -n "${DB_SSL_CA_PEM:-}" ] && [ -z "${DB_SSL_CA:-}" ]; then
    umask 077
    printf '%s\n' "$DB_SSL_CA_PEM" > /tmp/db-ca.pem
    export DB_SSL_CA=/tmp/db-ca.pem
fi

# Allow running one-off commands, e.g. `docker run IMAGE python manage.py check-config`.
if [ "$#" -gt 0 ]; then
    exec "$@"
fi

# Production mode rejects non-HTTPS requests, so the scheme and client address
# must come from the load balancer's X-Forwarded-* headers. Only expose the
# container to the load balancer, or narrow FORWARDED_ALLOW_IPS to its subnet.
exec uvicorn "$module:create_app" --factory \
    --host 0.0.0.0 \
    --port "${PORT:-8000}" \
    --workers "${WEB_CONCURRENCY:-2}" \
    --proxy-headers \
    --forwarded-allow-ips "${FORWARDED_ALLOW_IPS:-*}" \
    --no-access-log
