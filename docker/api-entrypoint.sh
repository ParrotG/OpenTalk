#!/bin/sh
set -eu

documents_dir=/app/data/documents
sessions_dir=/app/data/sessions
cache_dir=/home/app/.cache
default_uid=$(id -u app)
default_gid=$(id -g app)

mkdir -p "$documents_dir" "$sessions_dir" "$cache_dir"

# Bind-mounted documents should remain owned by the host user. Run the API with
# that numeric identity so uploaded artifacts stay usable from both environments.
runtime_uid=$(stat -c %u "$documents_dir")
runtime_gid=$(stat -c %g "$documents_dir")
if [ "$runtime_uid" -eq 0 ]; then
    runtime_uid=$default_uid
    runtime_gid=$default_gid
    chown "$runtime_uid:$runtime_gid" "$documents_dir"
fi

prepare_private_dir() {
    directory=$1
    owner=$(stat -c %u:%g "$directory")
    if [ "$owner" != "$runtime_uid:$runtime_gid" ]; then
        chown -R "$runtime_uid:$runtime_gid" "$directory"
    fi
}

prepare_private_dir "$sessions_dir"
prepare_private_dir "$cache_dir"

if ! setpriv --reuid "$runtime_uid" --regid "$runtime_gid" --clear-groups \
    sh -c 'test -w "$1"' check-write "$documents_dir"; then
    echo "The document storage directory is not writable: $documents_dir" >&2
    exit 1
fi

exec setpriv --reuid "$runtime_uid" --regid "$runtime_gid" --clear-groups "$@"
