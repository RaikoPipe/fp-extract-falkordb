#!/bin/sh
set -e

# The bind-mounted /app/data dir inherits the host owner's uid, which may
# differ from the container's appuser (uid 1001). Re-chown at runtime so
# appuser can create originals/ preprocessed/ etc. regardless of the host
# uid. Runs as root (the image's USER directive is removed; gosu drops
# privileges below).
chown -R appuser:appuser /app/data

# Drop to the non-root appuser and exec the original CMD.
exec gosu appuser "$@"