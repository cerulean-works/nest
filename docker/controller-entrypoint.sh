#!/bin/sh
set -eu

# Copy mounted credentials so host ownership does not need to match the
# container user. Only the private in-memory copy is chmod/chown'ed; the mounted
# directory is never written.
#
# Mode A SSH files may instead come from the environment (NEST_SSH_CONFIG,
# NEST_SSH_PRIVATE_KEY, NEST_SSH_KNOWN_HOSTS_DATA). nest.runtime_files writes them
# literally (no shell expansion) into the same private copy, refuses to replace a
# different file copied from the mount, and refuses weakened host-key settings.
# Without NEST_SSH_CONFIG it writes nothing.
materialize_ssh() {
    python -I -c 'import sys; from nest.runtime_files import main; sys.exit(main(sys.argv[1:]))' ssh "$@"
}

if [ "$(id -u)" = 0 ]; then
    mkdir -p /home/nest/.ssh
    if [ -d /run/nest-ssh ]; then
        cp -R /run/nest-ssh/. /home/nest/.ssh/
    fi
    materialize_ssh /home/nest/.ssh nest
    chown -R nest:nest /home/nest/.ssh
    find /home/nest/.ssh -type d -exec chmod 700 {} +
    find /home/nest/.ssh -type f -exec chmod 600 {} +
    exec gosu nest "$@"
fi

if [ -n "${NEST_SSH_CONFIG:-}" ]; then
    materialize_ssh "${HOME:?}/.ssh"
fi

exec "$@"
