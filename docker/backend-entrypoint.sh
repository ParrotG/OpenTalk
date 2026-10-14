#!/bin/sh
set -eu

documents_dir=/app/data/documents
sessions_dir=/app/data/sessions
logs_dir=/app/logs
cache_dir=/home/app/.cache
config_dir=/home/app/.config
data_dir=/home/app/.local/share
documents_access=${OPENTALK_DOCUMENTS_ACCESS:-read}
default_uid=$(id -u app)
default_gid=$(id -g app)

mkdir -p "$documents_dir" "$sessions_dir" "$logs_dir" "$cache_dir" "$config_dir" "$data_dir"

# API and worker share session data, so both derive one runtime identity from
# the host document mount instead of using different fixed container users.
runtime_uid=$(stat -c %u "$documents_dir")
runtime_gid=$(stat -c %g "$documents_dir")
if [ "$runtime_uid" -eq 0 ]; then
    runtime_uid=$default_uid
    runtime_gid=$default_gid
    if [ "$documents_access" = write ]; then
        chown "$runtime_uid:$runtime_gid" "$documents_dir"
    fi
fi
chown "$runtime_uid:$runtime_gid" /home/app

prepare_private_dir() {
    directory=$1
    owner=$(stat -c %u:%g "$directory")
    if [ "$owner" != "$runtime_uid:$runtime_gid" ]; then
        chown -R "$runtime_uid:$runtime_gid" "$directory"
    fi
}

prepare_private_dir "$sessions_dir"
prepare_private_dir "$logs_dir"
prepare_private_dir "$cache_dir"
prepare_private_dir "$config_dir"
prepare_private_dir "$data_dir"

for directory in "$sessions_dir" "$logs_dir" "$cache_dir" "$config_dir" "$data_dir"; do
    if ! setpriv --reuid "$runtime_uid" --regid "$runtime_gid" --clear-groups \
        sh -c 'test -w "$1"' check-write "$directory"; then
        echo "A required runtime directory is not writable: $directory" >&2
        exit 1
    fi
done

if [ "$documents_access" = write ]; then
    document_check=-w
else
    document_check=-r
fi
if ! setpriv --reuid "$runtime_uid" --regid "$runtime_gid" --clear-groups \
    test "$document_check" "$documents_dir"; then
    echo "The document storage directory does not allow required access: $documents_dir" >&2
    exit 1
fi

export HOME=/home/app
export XDG_CACHE_HOME=$cache_dir
export XDG_CONFIG_HOME=$config_dir
export XDG_DATA_HOME=$data_dir

exec setpriv --reuid "$runtime_uid" --regid "$runtime_gid" --clear-groups "$@"
