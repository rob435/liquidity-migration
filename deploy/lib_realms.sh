# The fleet's realm table: the realms it declares and every name derived from
# them. Sourced by deploy/lib_sleeves.sh, and shipped verbatim to the VPS by
# scripts/deploy_vps_live.sh so the remote body reads realm facts before its
# checkout exists. LM_REALM_TABLE_TEXT carries the table itself in that case.
# shellcheck shell=bash

LM_REALM_TABLE="${LM_REALM_TABLE:-deploy/realms.tsv}"

# ------------------------------------------------------------- realm table

# The realm table's data rows. LM_REALM_TABLE_TEXT wins over the file so a
# remote body can carry the table before its checkout exists.
lm_realm_rows() {
    if [ -z "${LM_REALM_TABLE_TEXT:-}" ] && [ ! -f "$LM_REALM_TABLE" ]; then
        echo "realm table is missing: $LM_REALM_TABLE" >&2
        return 1
    fi
    if [ -n "${LM_REALM_TABLE_TEXT:-}" ]; then
        printf '%s\n' "$LM_REALM_TABLE_TEXT"
    else
        cat "$LM_REALM_TABLE"
    fi | LC_ALL=C awk '!/^#/ && !/^[[:space:]]*$/ { print }'
}

lm_realms() { lm_realm_rows | LC_ALL=C awk -F '|' '{ print $1 }'; }

lm_funded_realms() {
    lm_realm_rows | LC_ALL=C awk -F '|' '$5 == "funded" { print $1 }'
}

# The one realm on a practice account: what the deploy soaks on.
lm_practice_realm() {
    lm_realm_rows | LC_ALL=C awk -F '|' '$5 == "practice" { print $1 }'
}

# `demo|mainnet|...` for the awk and case patterns below, in table order.
lm_realm_alternation() { lm_realms | paste -sd '|' -; }
lm_funded_alternation() { lm_funded_realms | paste -sd '|' -; }

lm_is_realm() {
    _lir_realm="$1"
    lm_realms | LC_ALL=C awk -v realm="$_lir_realm" '$0 == realm { found = 1 } END { exit found ? 0 : 1 }'
}

# One realm's declared column or derived name. Every derivation here has a
# twin in liquidity_migration/policy/realms.py; tests/policy/test_realms.py
# compares both for every field of every realm.
lm_realm_field() {
    _lrf_realm="$1"
    _lrf_field="$2"
    lm_realm_rows | LC_ALL=C awk -F '|' \
        -v want_realm="$_lrf_realm" -v want_field="$_lrf_field" '
function group_vars(names,   count, parts, index_group, index_name, out, group) {
    count = split(names, parts, " ")
    out = ""
    for (index_group = 1; index_group <= group_count; index_group++) {
        group = group_order[index_group]
        for (index_name = 1; index_name <= count; index_name++) {
            if (parts[index_name] != group) continue
            out = (out == "" ? group_variables[group] : out " " group_variables[group])
            break
        }
    }
    return out
}
function other_groups(kept,   index_group, group, out) {
    out = ""
    for (index_group = 1; index_group <= group_count; index_group++) {
        group = group_order[index_group]
        if (index(" " kept " ", " " group " ") > 0) continue
        out = (out == "" ? group : out " " group)
    }
    return out
}
BEGIN {
    group_count = split("bybit_demo bybit_real bybit_attest bybit_exclusive mexc_real hyperliquid", group_order, " ")
    group_variables["bybit_demo"] = "BYBIT_DEMO_API_KEY BYBIT_DEMO_API_SECRET"
    group_variables["bybit_real"] = "BYBIT_REAL_API_KEY BYBIT_REAL_API_SECRET BYBIT_REAL_API_KEY_IP BYBIT_REAL_API_KEY_BACKUP_IP"
    group_variables["bybit_attest"] = "BYBIT_ATTEST_API_KEY BYBIT_ATTEST_API_SECRET BYBIT_ATTEST_API_KEY_IP"
    group_variables["bybit_exclusive"] = "BYBIT_ENGINE_EXCLUSIVE_ACCOUNT_USER_ID"
    group_variables["mexc_real"] = "MEXC_REAL_API_KEY MEXC_REAL_API_SECRET"
    group_variables["hyperliquid"] = "HYPERLIQUID_REAL_ACCOUNT_ADDRESS HYPERLIQUID_REAL_API_WALLET_KEY HYPERLIQUID_TESTNET_ACCOUNT_ADDRESS HYPERLIQUID_TESTNET_API_WALLET_KEY"
    venue_groups["bybit"] = "bybit_demo bybit_real bybit_attest bybit_exclusive"
    venue_groups["mexc"] = "mexc_real"
    venue_groups["hyperliquid"] = "hyperliquid"
    kept_groups["bybit|practice"] = "bybit_demo"
    kept_groups["bybit|funded"] = "bybit_real bybit_exclusive"
    kept_groups["mexc|funded"] = "mexc_real"
    kept_groups["hyperliquid|funded"] = "hyperliquid"
    takeover["bybit|practice"] = "BYBIT_DEMO_API_KEY BYBIT_DEMO_API_SECRET"
    takeover["bybit|funded"] = "BYBIT_REAL_API_KEY BYBIT_REAL_API_SECRET BYBIT_REAL_API_KEY_IP BYBIT_REAL_API_KEY_BACKUP_IP BYBIT_ENGINE_EXCLUSIVE_ACCOUNT_USER_ID REAL_MONEY BYBIT_INVENTORY_CREDENTIAL_SET"
    takeover["mexc|funded"] = "MEXC_REAL_API_KEY MEXC_REAL_API_SECRET REAL_MONEY"
    takeover["hyperliquid|funded"] = "HYPERLIQUID_REAL_ACCOUNT_ADDRESS HYPERLIQUID_REAL_API_WALLET_KEY REAL_MONEY"
    venue_display["bybit"] = "Bybit"
    venue_display["mexc"] = "MEXC"
    venue_display["hyperliquid"] = "Hyperliquid"
    liveness_label["bybit"] = "MAINNET"
    liveness_label["mexc"] = "MEXC"
    liveness_label["hyperliquid"] = "Hyperliquid"
    telegram_label["bybit"] = "Real-money"
    attestor_groups["bybit"] = "bybit_attest bybit_exclusive"
    telegram_label["mexc"] = "MEXC"
    telegram_label["hyperliquid"] = "Hyperliquid"
    telegram_variables = "TELEGRAM_BOT_TOKEN TELEGRAM_CHAT_ID TELEGRAM_ALERT_CHAT_ID"
    public_data_venue = "bybit"
    for (index_group = 1; index_group <= group_count; index_group++) {
        all_groups = (all_groups == "" ? "" : all_groups " ") group_order[index_group]
    }
}
{
    rows[++row_count] = $0
    if (!($2 in seen_venue)) { seen_venue[$2] = 1; venue_order[++venue_count] = $2 }
}
END {
    for (row_index = 1; row_index <= row_count; row_index++) {
        split(rows[row_index], column, "|")
        if (column[1] != want_realm) continue
        realm = column[1]; venue = column[2]; kind = column[5]
        legacy = column[7]
        suffix = (legacy == "true" ? "" : "-" realm)
        engine_state = "liquidity-migration-engine" suffix
        worker_state = "liquidity-migration-signal-worker-" realm
        kept = kept_groups[venue "|" kind]
        others = other_groups(kept)
        scope = venue_groups[public_data_venue]
        if (venue != public_data_venue) {
            scope = ""
            for (venue_index = 1; venue_index <= venue_count; venue_index++) {
                scope = (scope == "" ? "" : scope " ") venue_groups[venue_order[venue_index]]
            }
        }
        value["realm"] = realm
        value["venue"] = venue
        value["engine_venue"] = column[3]
        value["engine_realm"] = column[4]
        value["kind"] = kind
        value["posture"] = column[6]
        value["legacy_names"] = legacy
        value["long_entries"] = column[8]
        value["carry_entries"] = column[9]
        value["exodus_entries"] = column[10]
        value["owner_stop"] = column[11]
        value["worker_stop"] = column[12]
        value["liveness_timer_stop"] = column[13]
        value["liveness_service_stop"] = column[14]
        value["funded"] = (kind == "funded" ? "true" : "false")
        value["activation"] = (kind == "funded" ? realm : "always")
        value["operator"] = (kind == "funded" ? "funded" : "direct")
        value["engine_unit"] = engine_state ".service"
        value["worker_unit"] = worker_state ".service"
        value["liveness_service"] = "liquidity-migration-" realm "-liveness.service"
        value["liveness_timer"] = "liquidity-migration-" realm "-liveness.timer"
        value["engine_user"] = "liquidity-engine-" realm
        value["engine_state_dir"] = "/var/lib/" engine_state
        value["engine_heartbeat"] = "/var/lib/" engine_state "/heartbeat.json"
        value["engine_wal"] = "/var/lib/" engine_state "/engine.wal"
        value["engine_env"] = "/etc/liquidity-migration/engine" suffix ".env"
        value["engine_config"] = "/etc/liquidity-migration/engine" suffix ".toml"
        value["engine_env_template"] = "deploy/engine" (legacy == "true" ? "" : "." realm) ".env.template"
        value["engine_toml_template"] = "deploy/engine." realm ".toml.template"
        value["worker_state_dir"] = "/var/lib/" worker_state
        value["worker_heartbeat"] = "/var/lib/" worker_state "/heartbeat.json"
        value["worker_env"] = "/etc/liquidity-migration/signal-worker-" realm ".env"
        value["worker_source_env"] = "/etc/liquidity-migration/signal-worker-" realm "-source.env"
        value["worker_source_dir"] = "/etc/liquidity-migration/signal-worker-" realm "-source"
        value["worker_profile"] = "/etc/liquidity-migration/signal-worker-" realm "-source/operational-profile.json"
        value["worker_env_template"] = "deploy/signal-worker-" realm ".env.template"
        value["worker_config"] = "/opt/liquidity-migration/configs/signal-worker." realm ".json"
        value["worker_config_repo"] = "configs/signal-worker." realm ".json"
        value["spool_dir"] = "/var/lib/liquidity-migration/signals/" realm
        value["control_dir"] = "/var/lib/liquidity-migration/controls/" realm
        value["liveness_state_dir"] = "/var/lib/liquidity-migration/liveness-" realm
        value["liveness_state_file"] = "/var/lib/liquidity-migration/liveness-" realm "/state.json"
        value["credential_env"] = "/etc/liquidity-migration/" venue "-" (kind == "funded" ? "mainnet" : "demo") ".env"
        value["telegram_env"] = "/etc/liquidity-migration/telegram-" realm ".env"
        value["long_root"] = "/opt/liquidity-migration/data/bybit-long-" realm "-event"
        value["carry_root"] = "/opt/liquidity-migration/data/bybit-carry-" realm "-event"
        value["exodus_root"] = "/opt/liquidity-migration/data/bybit-exodus-" realm "-event"
        value["preflight_command"] = (realm == "mainnet" ? "preflight" : "preflight-" realm)
        value["inventory_credential_set"] = (kind == "funded" ? "execution" : "demo")
        display = (venue in venue_display ? venue_display[venue] : toupper(substr(venue, 1, 1)) substr(venue, 2))
        value["venue_display"] = display
        value["liveness_label"] = (kind != "funded" ? "" : (venue in liveness_label ? liveness_label[venue] : display))
        value["telegram_label"] = (venue in telegram_label ? telegram_label[venue] : display)
        value["credential_vars"] = group_vars(kept)
        value["takeover_vars"] = takeover[venue "|" kind]
        value["engine_unset"] = group_vars(others) (kind == "funded" ? "" : " REAL_MONEY") " " telegram_variables
        value["worker_unset"] = group_vars(all_groups) " REAL_MONEY " telegram_variables
        value["liveness_unset"] = group_vars(scope) " REAL_MONEY"
        value["control_unset"] = group_vars(others) " REAL_MONEY " telegram_variables
        attestor = (kind == "funded" && (venue in attestor_groups) ? attestor_groups[venue] : "")
        value["attestor_env"] = (attestor == "" ? "" : \
            substr(value["credential_env"], 1, length(value["credential_env"]) - 4) "-attestor.env")
        value["control_unset_attestor"] = (attestor == "" ? "" : \
            group_vars(other_groups(attestor)) " REAL_MONEY " telegram_variables)
        if (!(want_field in value)) {
            print "unknown realm field: " want_field > "/dev/stderr"
            exit 3
        }
        if (kept == "" && want_field ~ /^(credential_vars|takeover_vars|engine_unset|control_unset)$/) {
            print "venue " venue " declares no credential families in lm_realm_field" > "/dev/stderr"
            exit 4
        }
        print value[want_field]
        exit 0
    }
    print "unknown realm: " want_realm > "/dev/stderr"
    exit 2
}
'
}
