#!/bin/sh
# The core/worker image's entrypoint (#161): keep the in-image data root owner-only,
# then run the command unchanged.
#
# The Dockerfile creates /var/lib/rheo-stream at mode 0700, and Docker copies that
# mode into a named volume the first time it mounts an empty one. A volume created
# before that change keeps its old mode (0755 on the flagship), and nothing in
# `resolve_data_root`/`validate_data_root` tightens an existing root: they create it
# 0700 if absent and only warn (`data_root_permissions_wide`) if it is wider. So this
# script tightens it at every container start, as the process user, before any
# `rheo` code reads it.
#
# Only the image's own path. An operator who points RHEO_DATA_ROOT somewhere else
# owns that directory's mode; the application still warns if it is too wide.
# A symlink is left alone: validate_data_root has its own rules for one, and chmod
# would follow it. A failed chmod is reported, not fatal, because the application
# already treats a wide root as a warning rather than a refusal.
root=/var/lib/rheo-stream
if [ -d "$root" ] && [ ! -L "$root" ]; then
    chmod 0700 "$root" || echo "core-entrypoint: could not chmod 0700 $root" >&2
fi
exec "$@"
