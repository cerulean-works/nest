#!/bin/sh
set -eu

# Copy mounted credentials so host ownership does not need to match the
# container user. Only the private in-memory copy is chmod/chown'ed.
if [ "$(id -u)" = 0 ]; then
    mkdir -p /home/nest/.ssh
    if [ -d /run/nest-ssh ]; then
        cp -R /run/nest-ssh/. /home/nest/.ssh/
    fi
    chown -R nest:nest /home/nest/.ssh
    find /home/nest/.ssh -type d -exec chmod 700 {} +
    find /home/nest/.ssh -type f -exec chmod 600 {} +
    exec gosu nest "$@"
fi

exec "$@"
