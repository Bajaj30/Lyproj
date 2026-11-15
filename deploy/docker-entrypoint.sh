#!/bin/sh
# Start as root only long enough to make the data volumes writable by the
# unprivileged `app` user, then run the server as that user.
set -e
if [ "$(id -u)" = "0" ]; then
    chown -R app:app /app/data /app/backend/uploads /app/ml_pipeline/.cache
    exec setpriv --reuid=app --regid=app --init-groups "$@"
fi
exec "$@"
