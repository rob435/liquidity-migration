# Strict private systemd EnvironmentFile loader for deploy/verify/recovery.
#
# Systemd values are data, not Bash source. The Python reader opens one stable,
# owner-matched, mode-0600 regular file and emits only caller-allowlisted values
# as NUL-delimited pairs. No value is evaluated or passed through argv.

lm_load_private_systemd_environment() {
    if [ "$#" -lt 3 ]; then
        echo "usage: lm_load_private_systemd_environment PYTHON FILE KEY [KEY ...]" >&2
        return 2
    fi
    local _lse_python="$1"
    local _lse_file="$2"
    shift 2
    local -a _lse_names=("$@")
    local -a _lse_command=(
        "$_lse_python" -m liquidity_migration.systemd_environment
        --path "$_lse_file"
    )
    local _lse_name _lse_key _lse_value _lse_fd _lse_pid
    local _lse_error=""
    local _lse_stream_failed=0
    local -A _lse_allowed=()
    local -A _lse_seen=()
    local -A _lse_values=()

    for _lse_name in "${_lse_names[@]}"; do
        if [[ ! "$_lse_name" =~ ^[A-Z][A-Z0-9_]*$ ]]; then
            echo "private systemd environment requested an invalid key" >&2
            return 2
        fi
        if [ "${_lse_allowed[$_lse_name]+present}" = present ]; then
            echo "private systemd environment requested a duplicate key: $_lse_name" >&2
            return 2
        fi
        _lse_allowed["$_lse_name"]=1
        _lse_command+=(--name "$_lse_name")
    done

    exec {_lse_fd}< <("${_lse_command[@]}")
    _lse_pid="$!"
    while IFS= read -r -d '' _lse_key <&"$_lse_fd"; do
        if ! IFS= read -r -d '' _lse_value <&"$_lse_fd"; then
            _lse_error="private systemd environment transfer ended with an incomplete pair"
            break
        fi
        if [ "${_lse_allowed[$_lse_key]+present}" != present ]; then
            _lse_error="private systemd environment transfer returned an unknown key"
            break
        fi
        if [ "${_lse_seen[$_lse_key]+present}" = present ]; then
            _lse_error="private systemd environment transfer repeated a key"
            break
        fi
        _lse_seen["$_lse_key"]=1
        _lse_values["$_lse_key"]="$_lse_value"
    done
    exec {_lse_fd}<&-
    if ! wait "$_lse_pid"; then
        _lse_stream_failed=1
    fi
    if [ -n "$_lse_error" ]; then
        echo "$_lse_error" >&2
        return 1
    fi
    if [ "$_lse_stream_failed" -ne 0 ]; then
        echo "private systemd environment transfer failed validation" >&2
        return 1
    fi

    # Commit the parsed snapshot only after the producer exited successfully.
    # Missing requested keys erase ambient values instead of inheriting them.
    for _lse_name in "${_lse_names[@]}"; do
        unset "$_lse_name"
        if [ "${_lse_seen[$_lse_name]+present}" = present ]; then
            printf -v "$_lse_name" '%s' "${_lse_values[$_lse_name]}"
            export "$_lse_name"
        fi
    done
}
