#!/bin/sh
set -e

# The bind-mounted /app/data dir inherits the host owner's uid, which may
# differ from the container's appuser (uid 1001). Re-chown at runtime so
# appuser can create originals/ preprocessed/ etc. regardless of the host
# uid. Runs as root (the image's USER directive is removed; gosu drops
# privileges below).
chown -R appuser:appuser /app/data

# Grant appuser access to the Docker socket (Docker-out-of-Docker).
# The socket is bind-mounted from the host.  gosu does not preserve
# supplementary groups, so we make the socket world-readable/writable
# instead of trying to match the host's docker group gid.
if [ -S /var/run/docker.sock ]; then
    chmod a+rw /var/run/docker.sock
fi

# Drop to the non-root appuser and exec the original CMD.
exec gosu appuser "$@"