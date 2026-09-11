"""The fleet's realm table, its derived names, and the files rendered from it.

`deploy/realms.tsv` is the only place a realm is declared. Everything that can
be spelled from the realm name by convention is derived here, not in the table:
unit names, users, state directories, spools, env and config paths, credential
files, data roots. The prose that differs per venue lives in `VENUE_FACTS`.

Run as a module: `render` writes every generated file, `check` exits 1 on drift.
Stdlib only — this runs on the host under the runtime venv.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

__all__ = [
    "CREDENTIAL_GROUPS",
    "GENERATED_MANIFEST_REGIONS",
    "REALM_TABLE",
    "Realm",
    "funded_realms",
    "main",
    "realm",
    "realm_fields",
    "realms",
    "render_realm_files",
    "venues",
]

_REPO_ROOT = Path(__file__).resolve().parents[2]

#: Repository-relative path of the realm table.
REALM_TABLE = "deploy/realms.tsv"

_TABLE_SCHEMA = "# realm-table-v1"
_TABLE_COLUMNS = (
    "realm",
    "venue",
    "engine_venue",
    "engine_realm",
    "kind",
    "posture",
    "legacy_names",
    "long_entries",
    "carry_entries",
    "exodus_entries",
    "owner_stop",
    "worker_stop",
    "liveness_timer_stop",
    "liveness_service_stop",
)
_TABLE_COLUMN_LINE = "# " + "|".join(_TABLE_COLUMNS)

#: The venue the fleet's first realms trade. Its workers carry no venue note
#: in their unit files, and its liveness units unset only its own credential
#: families; every later venue's units name their venue and unset every other
#: venue's credentials. Which public data a worker reads is `sources.public_venue`
#: in its configs/signal-worker.<realm>.json, not this constant.
PUBLIC_DATA_VENUE = "bybit"

#: Credential variable families, in the order every generated unset list uses.
#: A family belongs to one venue and is kept only by the realms that own it.
CREDENTIAL_GROUPS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("bybit_demo", ("BYBIT_DEMO_API_KEY", "BYBIT_DEMO_API_SECRET")),
    (
        "bybit_real",
        (
            "BYBIT_REAL_API_KEY",
            "BYBIT_REAL_API_SECRET",
            "BYBIT_REAL_API_KEY_IP",
            "BYBIT_REAL_API_KEY_BACKUP_IP",
        ),
    ),
    (
        "bybit_attest",
        ("BYBIT_ATTEST_API_KEY", "BYBIT_ATTEST_API_SECRET", "BYBIT_ATTEST_API_KEY_IP"),
    ),
    ("bybit_exclusive", ("BYBIT_ENGINE_EXCLUSIVE_ACCOUNT_USER_ID",)),
    ("mexc_real", ("MEXC_REAL_API_KEY", "MEXC_REAL_API_SECRET")),
    (
        "hyperliquid",
        (
            "HYPERLIQUID_REAL_ACCOUNT_ADDRESS",
            "HYPERLIQUID_REAL_API_WALLET_KEY",
            "HYPERLIQUID_TESTNET_ACCOUNT_ADDRESS",
            "HYPERLIQUID_TESTNET_API_WALLET_KEY",
        ),
    ),
)

TELEGRAM_VARS = ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "TELEGRAM_ALERT_CHAT_ID")

#: Per-venue facts and prose. `groups` names the credential families the venue
#: owns; `keeps` names the families one of its realms keeps, by kind. Every
#: string here is prose a unit file or env template quotes verbatim.
VENUE_FACTS: dict[str, dict[str, Any]] = {
    "bybit": {
        "display": "Bybit",
        "groups": ("bybit_demo", "bybit_real", "bybit_attest", "bybit_exclusive"),
        "keeps": {"practice": ("bybit_demo",), "funded": ("bybit_real", "bybit_exclusive")},
        "takeover_vars": {
            "practice": ("BYBIT_DEMO_API_KEY", "BYBIT_DEMO_API_SECRET"),
            "funded": (
                "BYBIT_REAL_API_KEY",
                "BYBIT_REAL_API_SECRET",
                "BYBIT_REAL_API_KEY_IP",
                "BYBIT_REAL_API_KEY_BACKUP_IP",
                "BYBIT_ENGINE_EXCLUSIVE_ACCOUNT_USER_ID",
                "REAL_MONEY",
                "BYBIT_INVENTORY_CREDENTIAL_SET",
            ),
        },
        "liveness_label": "MAINNET",
        "telegram_label": "Real-money",
        #: The venue's read-only key family, and what a run holding it keeps.
        "attestor_groups": ("bybit_attest", "bybit_exclusive"),
        "engine_account_phrase": "FUNDED account",
        "account_id_placeholder": "",
        "engine_lease_note": (
            "# NO `Conflicts=` with anything, deliberately. What stops two processes trading\n"
            "# this account is the account's kernel lease, which this engine takes at boot.\n"
            "# A systemd conflict knows nothing about that lease and would stop the funded\n"
            "# fleet the moment anyone started this unit."
        ),
        "engine_state_dir_note": (
            "# Not the deployed checkout: a relative log path inside /opt/liquidity-migration\n"
            "# would write an untracked file into the tree the deploy proves clean, and stop\n"
            "# the next deploy of the fleet. Its own directory, separate from the demo\n"
            "# engine's, so the two never share a log or a heartbeat."
        ),
        "engine_credential_note": (
            "# The funded account's credentials, and REAL_MONEY.\n"
            "#\n"
            "# This file is the owner's. `REAL_MONEY=true` in it is the single switch that\n"
            "# lets anything reach the funded account, it is set by the owner's own hand,\n"
            "# and deploy only ever rewrites it atomically to false during explicit disarm.\n"
            "# Without it the engine refuses to\n"
            "# build a mainnet gateway at all — it fails at the credential read, before a\n"
            "# socket is opened, and says which switch is missing."
        ),
        "engine_unset_note": (
            "# This owner receives its write-key family plus the exact account user id that\n"
            "# the gateway binds at startup. Demo and read-only attestor credentials are\n"
            "# removed even if they were present in PID 1's environment."
        ),
        "engine_env_note": (
            "# Config path and heartbeat path; deploy/engine.mainnet.env.template is the\n"
            "# copy to fill in. No live switch lives here: REAL_MONEY in the credential\n"
            "# file is what decides whether this unit runs at all."
        ),
        "engine_memory_note": (
            "# Boot replays the newest log segment and holds it decoded. Two measurements,\n"
            "# 2026-09-01: a 53 MB segment peaked at 322 MB, a 165 MB one at 522 MB. The\n"
            "# engine config rotates segments at 256 MB. MemoryMax=2G is set above an\n"
            "# extrapolation from those two points, not a third measurement."
        ),
        "liveness_observer_note": (
            "# Detection reads the manifest and published artifacts. Delivery reads only\n"
            "# dedicated route files; the funded account key and REAL_MONEY switch never\n"
            "# enter the observer's environment."
        ),
        "env_intro": (
            "# Unit settings for the Rust execution engine on the FUNDED account. Copy this\n"
            "# file to the path above on the VPS and edit it there; no deploy writes it."
        ),
        "env_credential_note": (
            "# The credentials are NOT here. They are in\n"
            "# /etc/liquidity-migration/bybit-mainnet.env, along with REAL_MONEY, and that\n"
            "# file is the owner's — nothing in this repository writes it, and without\n"
            "# REAL_MONEY=true in it the engine refuses to build a mainnet gateway at all."
        ),
        "env_config_note": (
            "# Ordered native strategies, account limits, WAL paths, and the compiled-in\n"
            "# funded venue adapter. Rollout renders and checks this file from registered\n"
            "# rules and the installed operational profile."
        ),
        "env_identity_note": (
            "# Required identity binding. Obtain the exact numeric/string user id from a\n"
            "# read-only authenticated venue response; never infer it from an API-key label.\n"
            "# Activation refuses a missing value or a heartbeat from another account."
        ),
        "env_realm_note": "",
        "env_section_gap": "\n",
        "env_watched_note": (
            "# The fleet's watchdog (check_fleet_liveness.py, every three minutes) reads the\n"
            "# heartbeat file and pages when it goes stale, cannot be read, or says the\n"
            "# engine has stopped opening positions.\n"
            "#\n"
            "# THIS MUST BE THE SAME PATH as `heartbeat_path` in the [engine] block of the\n"
            "# config, and nothing checks that for you: the engine reads its path from the\n"
            "# config, the watchdog reads it from here. Two different paths means the engine\n"
            "# writes a file nobody reads and the watchdog pages about a file nobody writes.\n"
            "#\n"
            "# Note this is a different file from the demo engine's. Two engines on two\n"
            "# accounts writing one heartbeat would each overwrite the other's, and the\n"
            "# watchdog would report whichever wrote last as if it were both."
        ),
        "worker_env_note": (
            "# Reviewed public inputs copied into the unprivileged funded Rust signal worker.\n"
            "# This file contains no venue credentials."
        ),
    },
    "mexc": {
        "display": "MEXC",
        "groups": ("mexc_real",),
        "keeps": {"funded": ("mexc_real",)},
        "takeover_vars": {
            "funded": ("MEXC_REAL_API_KEY", "MEXC_REAL_API_SECRET", "REAL_MONEY"),
        },
        "liveness_label": "MEXC",
        "telegram_label": "MEXC",
        "engine_account_phrase": "FUNDED MEXC account",
        "account_id_placeholder": "uid-",
        "engine_credential_note": (
            "# The MEXC account's credentials, and REAL_MONEY.\n"
            "#\n"
            "# MEXC publishes no futures testnet: this key always reaches real capital.\n"
            "# `REAL_MONEY=true` in this file is the single switch that lets anything reach\n"
            "# the account, it is set by the owner's own hand, and deploy only ever rewrites\n"
            "# it atomically to false during explicit disarm."
        ),
        "engine_unset_note": (
            "# This owner receives the MEXC pair and the exact account id the gateway binds\n"
            "# at startup. Every Bybit and Hyperliquid credential is removed even if it was\n"
            "# in PID 1's environment."
        ),
        "engine_env_note": (
            "# Config path and heartbeat path; deploy/engine.mexc.env.template is the copy\n"
            "# to fill in. No live switch lives here: REAL_MONEY in the credential file is\n"
            "# what decides whether this unit runs at all."
        ),
        "liveness_observer_note": (
            "# Detection reads the manifest and published artifacts. Delivery reads only\n"
            "# dedicated route files; the MEXC account key and REAL_MONEY switch never\n"
            "# enter the observer's environment."
        ),
        "env_intro": (
            "# Unit settings for the Rust execution engine on the MEXC account. Copy this\n"
            "# file to the path above on the VPS and edit it there; no deploy writes it."
        ),
        "env_credential_note": (
            "# The credentials are NOT here. They are in\n"
            "# /etc/liquidity-migration/mexc-mainnet.env, along with REAL_MONEY, and that\n"
            "# file is the owner's — nothing in this repository writes it, and without\n"
            "# REAL_MONEY=true in it the engine refuses to build a MEXC gateway at all."
        ),
        "env_config_note": (
            "# Ordered native strategies, account limits, WAL paths, and the compiled-in\n"
            "# MEXC adapter. Rollout renders and checks this file from registered rules and\n"
            "# the installed operational profile."
        ),
        "env_identity_note": (
            "# Required identity binding: `uid-` plus the physical account UID that the\n"
            "# root-owned registry /etc/liquidity-migration/mexc-account-bindings.json binds\n"
            "# this key's sha256 fingerprint to. Without that registry the gateway does not\n"
            "# construct. `engine verify-account-identity --config /etc/liquidity-migration/engine-mexc.toml`\n"
            "# prints the exact string this must equal. Never infer it from a key label.\n"
            "# Activation refuses a missing value or a heartbeat from another account."
        ),
        "env_realm_note": (
            "# The venue name carries the realm: `mexc_mainnet` is the only MEXC realm and\n"
            "# it is the string the heartbeat and the account lease are named by.\n"
        ),
        "env_section_gap": "",
        "worker_env_note": (
            "# Reviewed public inputs copied into the unprivileged Rust signal worker that\n"
            "# feeds the MEXC account owner. This file contains no venue credentials; the\n"
            "# public data is MEXC's own (`sources.public_venue` in\n"
            "# configs/signal-worker.mexc.json)."
        ),
    },
    "hyperliquid": {
        "display": "Hyperliquid",
        "groups": ("hyperliquid",),
        "keeps": {"funded": ("hyperliquid",)},
        "takeover_vars": {
            "funded": (
                "HYPERLIQUID_REAL_ACCOUNT_ADDRESS",
                "HYPERLIQUID_REAL_API_WALLET_KEY",
                "REAL_MONEY",
            ),
        },
        "liveness_label": "Hyperliquid",
        "telegram_label": "Hyperliquid",
        "engine_account_phrase": "FUNDED Hyperliquid account",
        "account_id_placeholder": "0x",
        "engine_credential_note": (
            "# The Hyperliquid account's address and API wallet key, and REAL_MONEY.\n"
            "#\n"
            "# Hyperliquid publishes a testnet, but this realm is the funded mainnet account\n"
            "# only. `REAL_MONEY=true` in this file is the single switch that lets anything\n"
            "# reach the account, it is set by the owner's own hand, and deploy only ever\n"
            "# rewrites it atomically to false during explicit disarm."
        ),
        "engine_unset_note": (
            "# This owner receives the Hyperliquid pair and the exact account address the\n"
            "# gateway binds at startup. Every Bybit and MEXC credential is removed even if\n"
            "# it was in PID 1's environment."
        ),
        "engine_env_note": (
            "# Config path and heartbeat path; deploy/engine.hyperliquid.env.template is the\n"
            "# copy to fill in. No live switch lives here: REAL_MONEY in the credential file\n"
            "# is what decides whether this unit runs at all."
        ),
        "liveness_observer_note": (
            "# Detection reads the manifest and published artifacts. Delivery reads only\n"
            "# dedicated route files; the Hyperliquid API wallet key and REAL_MONEY switch\n"
            "# never enter the observer's environment."
        ),
        "env_intro": (
            "# Unit settings for the Rust execution engine on the Hyperliquid account. Copy\n"
            "# this file to the path above on the VPS and edit it there; no deploy writes it."
        ),
        "env_credential_note": (
            "# The credentials are NOT here. They are in\n"
            "# /etc/liquidity-migration/hyperliquid-mainnet.env, along with REAL_MONEY, and\n"
            "# that file is the owner's — nothing in this repository writes it, and without\n"
            "# REAL_MONEY=true in it the engine refuses to build a Hyperliquid gateway at\n"
            "# all."
        ),
        "env_config_note": (
            "# Ordered native strategies, account limits, WAL paths, and the compiled-in\n"
            "# Hyperliquid adapter. Rollout renders and checks this file from registered\n"
            "# rules and the installed operational profile."
        ),
        "env_identity_note": (
            "# Required identity binding. On Hyperliquid the account id is the master\n"
            "# account's own address, lowercase `0x` plus 40 hex characters — not the API\n"
            "# wallet's address.\n"
            "# `engine verify-account-identity --config /etc/liquidity-migration/engine-hyperliquid.toml`\n"
            "# prints the exact string this must equal. The `0x` below is a deliberate\n"
            "# placeholder: it mismatches every account, so activation refuses until the\n"
            "# owner pastes what that command printed."
        ),
        "env_realm_note": (
            "# The venue name carries the realm: `hyperliquid_mainnet` is the only\n"
            "# Hyperliquid realm the fleet runs, and it is the string the heartbeat and the\n"
            "# account lease are named by.\n"
        ),
        "env_section_gap": "",
        "worker_env_note": (
            "# Reviewed public inputs copied into the unprivileged Rust signal worker that\n"
            "# feeds the Hyperliquid account owner. This file contains no venue credentials;\n"
            "# the public data is Hyperliquid's own (`sources.public_venue` in\n"
            "# configs/signal-worker.hyperliquid.json)."
        ),
    },
}

#: Prose a single realm owns: an account number, a neighbour it must not share
#: state with. Everything else about a realm comes from its venue or its name.
REALM_PROSE: dict[str, dict[str, str]] = {
    "demo": {
        "engine_account_note": (
            "# The fleet's demo account, 555899665. Native directional reducers and order\n"
            "# ownership live in this process, so it must not use the separate quote-lab\n"
            "# account, 579580669 in bybit-quote-lab.env. Two writers on one venue account\n"
            "# wedge each other; the kernel lease makes this owner refuse a competing writer.\n"
            "#\n"
            "# The Telegram tokens in this file are unset below. The engine sends no\n"
            "# messages, and a process that cannot reach the owner's chat cannot post to it\n"
            "# by accident."
        ),
        "env_account_note": (
            "# The demo engine is mandatory on a deployed fleet. Its credentials remain in\n"
            "# the separate file below and are never copied into this non-secret projection.\n"
            "#\n"
            "# The credentials are NOT here: the unit loads them from\n"
            "# /etc/liquidity-migration/bybit-demo.env — the fleet's demo account\n"
            "# (555899665), the live demo book — not the second demo account, 579580669 in\n"
            "# bybit-quote-lab.env. The engine is this account's only writer and holds its\n"
            "# single-writer lease."
        ),
        "env_account_id": "555899665",
    },
}

#: What a venue that declares no prose of its own says instead. `{display}`,
#: `{realm}`, `{credential_env}` and `{engine_env_template}` are filled per realm,
#: so adding a realm on a new venue needs only the venue's credential families.
_DEFAULT_VENUE_FACTS: dict[str, str] = {
    "account_id_placeholder": "",
    "engine_account_phrase": "FUNDED {display} account",
    "engine_lease_note": (
        "# NO `Conflicts=` with anything, deliberately. What stops two processes trading\n"
        "# this account is the account's kernel lease, which this engine takes at boot."
    ),
    "engine_state_dir_note": (
        "# Not the deployed checkout: a relative log path inside /opt/liquidity-migration\n"
        "# would write an untracked file into the tree the deploy proves clean. Its own\n"
        "# directory, so no two engines share a log, a WAL or a heartbeat."
    ),
    "engine_credential_note": (
        "# The {display} account's credentials, and REAL_MONEY.\n"
        "#\n"
        "# `REAL_MONEY=true` in this file is the single switch that lets anything reach\n"
        "# the account, it is set by the owner's own hand, and deploy only ever rewrites\n"
        "# it atomically to false during explicit disarm."
    ),
    "engine_unset_note": (
        "# This owner receives the {display} credentials and the exact account id the\n"
        "# gateway binds at startup. Every other venue's credential is removed even if it\n"
        "# was in PID 1's environment."
    ),
    "engine_env_note": (
        "# Config path and heartbeat path; {engine_env_template} is the copy to fill in.\n"
        "# No live switch lives here: REAL_MONEY in the credential file is what decides\n"
        "# whether this unit runs at all."
    ),
    "engine_memory_note": (
        "# Boot replays the newest log segment and holds it decoded. Two measurements,\n"
        "# 2026-09-01: a 53 MB segment peaked at 322 MB, a 165 MB one at 522 MB. The\n"
        "# engine config rotates segments at 256 MB. MemoryMax=2G is set above an\n"
        "# extrapolation from those two points, not a third measurement."
    ),
    "liveness_observer_note": (
        "# Detection reads the manifest and published artifacts. Delivery reads only\n"
        "# dedicated route files; the {display} account key and REAL_MONEY switch never\n"
        "# enter the observer's environment."
    ),
    "env_intro": (
        "# Unit settings for the Rust execution engine on the {display} account. Copy\n"
        "# this file to the path above on the VPS and edit it there; no deploy writes it."
    ),
    "env_credential_note": (
        "# The credentials are NOT here. They are in\n"
        "# {credential_env}, along with REAL_MONEY, and that file is the owner's —\n"
        "# nothing in this repository writes it, and without REAL_MONEY=true in it the\n"
        "# engine refuses to build a {display} gateway at all."
    ),
    "env_config_note": (
        "# Ordered native strategies, account limits, WAL paths, and the compiled-in\n"
        "# {display} adapter. Rollout renders and checks this file from registered rules\n"
        "# and the installed operational profile."
    ),
    "env_identity_note": (
        "# Required identity binding. Obtain the exact account id from a read-only\n"
        "# authenticated venue response; never infer it from an API-key label.\n"
        "# Activation refuses a missing value or a heartbeat from another account."
    ),
    "env_realm_note": "",
    "env_section_gap": "",
    "env_watched_note": (
        "# The fleet's watchdog (check_fleet_liveness.py) reads the heartbeat file and\n"
        "# pages when it goes stale, cannot be read, or says the engine has stopped\n"
        "# opening positions.\n"
        "#\n"
        "# THIS MUST BE THE SAME PATH as `heartbeat_path` in the [engine] block of the\n"
        "# config, and nothing checks that for you: the engine reads its path from the\n"
        "# config, the watchdog reads it from here. Two different paths means the engine\n"
        "# writes a file nobody reads and the watchdog pages about a file nobody writes."
    ),
    "worker_env_note": (
        "# Reviewed public inputs copied into the unprivileged Rust signal worker that\n"
        "# feeds the {display} account owner. This file contains no venue credentials; the\n"
        "# public data comes from the venue `sources.public_venue` names in that realm's\n"
        "# configs/signal-worker.<realm>.json."
    ),
}


@dataclass(frozen=True, slots=True)
class Realm:
    """One realm: its declared columns and every name derived from them."""

    realm: str
    venue: str
    engine_venue: str
    engine_realm: str
    kind: str
    posture: str
    legacy_names: bool
    long_entries: str
    carry_entries: str
    exodus_entries: str
    owner_stop: int
    worker_stop: int
    liveness_timer_stop: int
    liveness_service_stop: int
    #: Every venue the same table names, in the order it first names them.
    venue_order: tuple[str, ...]

    @property
    def funded(self) -> bool:
        return self.kind == "funded"

    @property
    def suffix(self) -> str:
        """What the engine's own names carry. Empty only for the legacy demo names."""
        return "" if self.legacy_names else f"-{self.realm}"

    @property
    def activation(self) -> str:
        return self.realm if self.funded else "always"

    @property
    def operator(self) -> str:
        return "funded" if self.funded else "direct"

    @property
    def engine_unit(self) -> str:
        return f"liquidity-migration-engine{self.suffix}.service"

    @property
    def worker_unit(self) -> str:
        return f"liquidity-migration-signal-worker-{self.realm}.service"

    @property
    def liveness_service(self) -> str:
        return f"liquidity-migration-{self.realm}-liveness.service"

    @property
    def liveness_timer(self) -> str:
        return f"liquidity-migration-{self.realm}-liveness.timer"

    @property
    def engine_user(self) -> str:
        return f"liquidity-engine-{self.realm}"

    @property
    def engine_state_dir_name(self) -> str:
        return f"liquidity-migration-engine{self.suffix}"

    @property
    def engine_state_dir(self) -> str:
        return f"/var/lib/{self.engine_state_dir_name}"

    @property
    def engine_heartbeat(self) -> str:
        return f"{self.engine_state_dir}/heartbeat.json"

    @property
    def engine_wal(self) -> str:
        return f"{self.engine_state_dir}/engine.wal"

    @property
    def engine_env(self) -> str:
        return f"/etc/liquidity-migration/engine{self.suffix}.env"

    @property
    def engine_config(self) -> str:
        return f"/etc/liquidity-migration/engine{self.suffix}.toml"

    @property
    def engine_env_template(self) -> str:
        infix = "" if self.legacy_names else f".{self.realm}"
        return f"deploy/engine{infix}.env.template"

    @property
    def engine_toml_template(self) -> str:
        return f"deploy/engine.{self.realm}.toml.template"

    @property
    def worker_state_dir_name(self) -> str:
        return f"liquidity-migration-signal-worker-{self.realm}"

    @property
    def worker_state_dir(self) -> str:
        return f"/var/lib/{self.worker_state_dir_name}"

    @property
    def worker_heartbeat(self) -> str:
        return f"{self.worker_state_dir}/heartbeat.json"

    @property
    def worker_env(self) -> str:
        return f"/etc/liquidity-migration/signal-worker-{self.realm}.env"

    @property
    def worker_source_env(self) -> str:
        return f"/etc/liquidity-migration/signal-worker-{self.realm}-source.env"

    @property
    def worker_source_dir(self) -> str:
        return f"/etc/liquidity-migration/signal-worker-{self.realm}-source"

    @property
    def worker_profile(self) -> str:
        return f"{self.worker_source_dir}/operational-profile.json"

    @property
    def worker_env_template(self) -> str:
        return f"deploy/signal-worker-{self.realm}.env.template"

    @property
    def worker_config(self) -> str:
        return f"/opt/liquidity-migration/configs/signal-worker.{self.realm}.json"

    @property
    def worker_config_repo(self) -> str:
        return f"configs/signal-worker.{self.realm}.json"

    @property
    def spool_dir(self) -> str:
        return f"/var/lib/liquidity-migration/signals/{self.realm}"

    @property
    def control_dir(self) -> str:
        return f"/var/lib/liquidity-migration/controls/{self.realm}"

    @property
    def liveness_state_dir(self) -> str:
        return f"/var/lib/liquidity-migration/liveness-{self.realm}"

    @property
    def liveness_state_file(self) -> str:
        return f"{self.liveness_state_dir}/state.json"

    @property
    def credential_env(self) -> str:
        slug = "mainnet" if self.funded else "demo"
        return f"/etc/liquidity-migration/{self.venue}-{slug}.env"

    @property
    def telegram_env(self) -> str:
        return f"/etc/liquidity-migration/telegram-{self.realm}.env"

    @property
    def long_root(self) -> str:
        return f"/opt/liquidity-migration/data/bybit-long-{self.realm}-event"

    @property
    def carry_root(self) -> str:
        return f"/opt/liquidity-migration/data/bybit-carry-{self.realm}-event"

    @property
    def exodus_root(self) -> str:
        return f"/opt/liquidity-migration/data/bybit-exodus-{self.realm}-event"

    @property
    def preflight_command(self) -> str:
        """The operator's arming verb. `mainnet` predates the suffixed ones."""
        return "preflight" if self.realm == "mainnet" else f"preflight-{self.realm}"

    @property
    def inventory_credential_set(self) -> str:
        return "execution" if self.funded else "demo"

    @property
    def venue_display(self) -> str:
        facts = VENUE_FACTS.get(self.venue, {})
        return str(facts.get("display") or self.venue[:1].upper() + self.venue[1:])

    @property
    def liveness_label(self) -> str:
        if not self.funded:
            return ""
        facts = VENUE_FACTS.get(self.venue, {})
        return str(facts.get("liveness_label") or self.venue_display)

    @property
    def telegram_label(self) -> str:
        facts = VENUE_FACTS.get(self.venue, {})
        return str(facts.get("telegram_label") or self.venue_display)

    @property
    def kept_groups(self) -> tuple[str, ...]:
        keeps = VENUE_FACTS.get(self.venue, {}).get("keeps", {})
        assert isinstance(keeps, dict)
        return tuple(keeps.get(self.kind, ()))

    @property
    def credential_vars(self) -> tuple[str, ...]:
        return _group_vars(self.kept_groups)

    @property
    def takeover_vars(self) -> tuple[str, ...]:
        table = VENUE_FACTS.get(self.venue, {}).get("takeover_vars", {})
        assert isinstance(table, dict)
        return tuple(table.get(self.kind, ()))

    @property
    def engine_unset(self) -> tuple[str, ...]:
        """Every credential family this owner does not hold, then the switch and routes."""
        other = tuple(name for name, _ in CREDENTIAL_GROUPS if name not in self.kept_groups)
        real_money = () if self.funded else ("REAL_MONEY",)
        return _group_vars(other) + real_money + TELEGRAM_VARS

    @property
    def worker_unset(self) -> tuple[str, ...]:
        every = tuple(name for name, _ in CREDENTIAL_GROUPS)
        return _group_vars(every) + ("REAL_MONEY",) + TELEGRAM_VARS

    @property
    def liveness_scope(self) -> tuple[str, ...]:
        """Which venues' credentials the observer refuses. Bybit realms predate
        the other venues and still name only Bybit's."""
        if self.venue == PUBLIC_DATA_VENUE:
            return (PUBLIC_DATA_VENUE,)
        return self.venue_order

    @property
    def liveness_unset(self) -> tuple[str, ...]:
        groups: list[str] = []
        for venue in self.liveness_scope:
            groups.extend(str(name) for name in VENUE_FACTS.get(venue, {}).get("groups", ()))
        return _group_vars(tuple(groups)) + ("REAL_MONEY",)

    @property
    def control_unset(self) -> tuple[str, ...]:
        """`ops.sh` engine control: every other venue's keys, the switch, the routes."""
        other = tuple(name for name, _ in CREDENTIAL_GROUPS if name not in self.kept_groups)
        return _group_vars(other) + ("REAL_MONEY",) + TELEGRAM_VARS

    @property
    def attestor_groups(self) -> tuple[str, ...]:
        """The read-only key family a funded realm's venue publishes, if any."""
        if not self.funded:
            return ()
        return tuple(VENUE_FACTS.get(self.venue, {}).get("attestor_groups", ()))

    @property
    def attestor_env(self) -> str:
        """The owner's read-only credential file, or empty when the venue has none."""
        if not self.attestor_groups:
            return ""
        return self.credential_env.removesuffix(".env") + "-attestor.env"

    @property
    def control_unset_attestor(self) -> tuple[str, ...]:
        """The same list for a run holding the read-only family instead."""
        if not self.attestor_groups:
            return ()
        other = tuple(
            name for name, _ in CREDENTIAL_GROUPS if name not in self.attestor_groups
        )
        return _group_vars(other) + ("REAL_MONEY",) + TELEGRAM_VARS


def _group_vars(names: tuple[str, ...] | list[str]) -> tuple[str, ...]:
    wanted = set(names)
    out: list[str] = []
    for name, variables in CREDENTIAL_GROUPS:
        if name in wanted:
            out.extend(variables)
    return tuple(out)


def _parse_bool(value: str, *, column: str, line: int) -> bool:
    if value not in {"true", "false"}:
        raise ValueError(f"{REALM_TABLE}:{line}: {column} must be true or false")
    return value == "true"


def _parse_entries(value: str, *, column: str, line: int) -> str:
    if value not in {"true", "false", "toggles"}:
        raise ValueError(f"{REALM_TABLE}:{line}: {column} must be true, false or toggles")
    return value


def parse_realm_table(text: str) -> tuple[Realm, ...]:
    """Parse the table's text. The only reader of its schema."""

    lines = text.splitlines()
    if not lines or lines[0] != _TABLE_SCHEMA:
        raise ValueError(f"{REALM_TABLE}: unsupported schema; expected {_TABLE_SCHEMA}")
    if len(lines) < 2 or lines[1] != _TABLE_COLUMN_LINE:
        raise ValueError(f"{REALM_TABLE}: column contract is invalid")
    parsed: list[Realm] = []
    for offset, raw in enumerate(lines[2:], start=3):
        if not raw.strip() or raw.startswith("#"):
            continue
        fields = raw.split("|")
        if len(fields) != len(_TABLE_COLUMNS):
            raise ValueError(
                f"{REALM_TABLE}:{offset}: expected {len(_TABLE_COLUMNS)} fields, found {len(fields)}"
            )
        row = dict(zip(_TABLE_COLUMNS, fields))
        if row["kind"] not in {"practice", "funded"}:
            raise ValueError(f"{REALM_TABLE}:{offset}: kind must be practice or funded")
        if row["posture"] not in {"running", "stopped"}:
            raise ValueError(f"{REALM_TABLE}:{offset}: posture must be running or stopped")
        if row["venue"] not in VENUE_FACTS:
            raise ValueError(f"{REALM_TABLE}:{offset}: venue {row['venue']} has no VENUE_FACTS")
        if row["kind"] == "practice" and row["posture"] != "running":
            raise ValueError(
                f"{REALM_TABLE}:{offset}: a practice realm carries the deploy soak and must run"
            )
        if row["legacy_names"] == "true" and row["kind"] != "practice":
            raise ValueError(
                f"{REALM_TABLE}:{offset}: only the practice realm carries the unsuffixed names"
            )
        parsed.append(
            Realm(
                realm=row["realm"],
                venue=row["venue"],
                engine_venue=row["engine_venue"],
                engine_realm=row["engine_realm"],
                kind=row["kind"],
                posture=row["posture"],
                legacy_names=_parse_bool(row["legacy_names"], column="legacy_names", line=offset),
                long_entries=_parse_entries(row["long_entries"], column="long_entries", line=offset),
                carry_entries=_parse_entries(
                    row["carry_entries"], column="carry_entries", line=offset
                ),
                exodus_entries=_parse_entries(
                    row["exodus_entries"], column="exodus_entries", line=offset
                ),
                owner_stop=int(row["owner_stop"]),
                worker_stop=int(row["worker_stop"]),
                liveness_timer_stop=int(row["liveness_timer_stop"]),
                liveness_service_stop=int(row["liveness_service_stop"]),
                venue_order=(),
            )
        )
    if not parsed:
        raise ValueError(f"{REALM_TABLE}: no realms")
    names = [row.realm for row in parsed]
    if len(set(names)) != len(names):
        raise ValueError(f"{REALM_TABLE}: duplicate realm")
    if sum(1 for row in parsed if not row.funded) != 1:
        raise ValueError(f"{REALM_TABLE}: exactly one practice realm carries the deploy soak")
    order: list[str] = []
    for entry in parsed:
        if entry.venue not in order:
            order.append(entry.venue)
    return tuple(replace(entry, venue_order=tuple(order)) for entry in parsed)


def realms(root: Path | None = None) -> tuple[Realm, ...]:
    """Every realm, in table order."""

    base = _REPO_ROOT if root is None else root
    return parse_realm_table((base / REALM_TABLE).read_text(encoding="utf-8"))


def funded_realms(root: Path | None = None) -> tuple[Realm, ...]:
    return tuple(row for row in realms(root) if row.funded)


def realm(name: str, root: Path | None = None) -> Realm:
    for row in realms(root):
        if row.realm == name:
            return row
    raise KeyError(f"{name} is not a realm in {REALM_TABLE}")


def venues(root: Path | None = None) -> tuple[str, ...]:
    """Venue names in the order the table first names them."""

    seen: list[str] = []
    for row in realms(root):
        if row.venue not in seen:
            seen.append(row.venue)
    return tuple(seen)


#: Every field `lm_realm_field` answers, and this module derives. The parity
#: test compares both sides for every realm.
_FIELDS = (
    "realm",
    "venue",
    "engine_venue",
    "engine_realm",
    "kind",
    "posture",
    "legacy_names",
    "long_entries",
    "carry_entries",
    "exodus_entries",
    "owner_stop",
    "worker_stop",
    "liveness_timer_stop",
    "liveness_service_stop",
    "funded",
    "activation",
    "operator",
    "engine_unit",
    "worker_unit",
    "liveness_service",
    "liveness_timer",
    "engine_user",
    "engine_state_dir",
    "engine_heartbeat",
    "engine_wal",
    "engine_env",
    "engine_config",
    "engine_env_template",
    "engine_toml_template",
    "worker_state_dir",
    "worker_heartbeat",
    "worker_env",
    "worker_source_env",
    "worker_source_dir",
    "worker_profile",
    "worker_env_template",
    "worker_config",
    "worker_config_repo",
    "spool_dir",
    "control_dir",
    "liveness_state_dir",
    "liveness_state_file",
    "credential_env",
    "telegram_env",
    "long_root",
    "carry_root",
    "exodus_root",
    "preflight_command",
    "inventory_credential_set",
    "venue_display",
    "liveness_label",
    "telegram_label",
    "credential_vars",
    "takeover_vars",
    "engine_unset",
    "worker_unset",
    "liveness_unset",
    "control_unset",
    "attestor_env",
    "control_unset_attestor",
)


def realm_fields(row: Realm) -> dict[str, str]:
    """One realm as the flat strings `lm_realm_field` prints."""

    out: dict[str, str] = {}
    for field in _FIELDS:
        value = getattr(row, field)
        if isinstance(value, bool):
            out[field] = "true" if value else "false"
        elif isinstance(value, tuple):
            out[field] = " ".join(value)
        else:
            out[field] = str(value)
    return out


# --------------------------------------------------------------- unit files

_ENGINE_UNIT_PRACTICE = """\
[Unit]
StartLimitIntervalSec=300
StartLimitBurst=5
Description=liquidity-migration Rust execution engine on the fleet's {realm} account
Wants=network-online.target {worker_unit}
# The worker publishes durable public observations before this account owner
# consumes them. `Wants` starts it with the engine, but a later worker failure
# never stops exits or reconciliation in the account owner.
After=network-online.target {worker_unit}

[Service]
Type=notify
NotifyAccess=main
WatchdogSec=30
TimeoutStartSec=180
User={engine_user}
Group=liquidity-migration
# Not the deployed checkout. The engine's log path in engine.toml may be
# relative, and a relative path resolved inside /opt/liquidity-migration writes
# an untracked file into the tree the deploy proves clean at every exact-commit
# step — which would stop the next deploy of the funded fleet. systemd creates
# this directory before the engine starts.
StateDirectory={engine_state_dir_name}
WorkingDirectory={engine_state_dir}
{account_note}
EnvironmentFile={credential_env}
UnsetEnvironment={engine_unset}
# Config path and heartbeat path. {engine_env_template} is the copy to
# fill in.
EnvironmentFile={engine_env}
ExecStart=/opt/liquidity-migration-engine/bin/engine run --config ${{ENGINE_CONFIG_FILE}}
# 143 is a clean SIGTERM stop.
SuccessExitStatus=143
Restart=always
RestartSec=5
KillMode=control-group
# The engine finishes its current write and drops its account lock on the way
# out; nothing here waits on a venue round trip.
TimeoutStopSec=30
# Ahead of the credential-free signal worker (5), because this process carries
# orders on the {realm} account and an acquisition/backfill burst must not delay an
# exit. Still behind the funded engine (-5) on a two-core box.
Nice=0
IOSchedulingClass=best-effort
IOSchedulingPriority=4
NoNewPrivileges=true
PrivateTmp=true
ProtectProc=invisible
ProcSubset=pid
ProtectSystem=strict
ProtectHome=true
UMask=0027
ReadWritePaths=/run/lock/liquidity-migration {engine_state_dir} {spool_dir} {control_dir}
{memory_note}
MemoryMax=2G
LimitNOFILE=65536
TasksMax=256

[Install]
WantedBy=multi-user.target
"""

_ENGINE_UNIT_FUNDED = """\
[Unit]
StartLimitIntervalSec=300
StartLimitBurst=5
Description=liquidity-migration Rust execution engine on the {account_phrase} (runs only while REAL_MONEY is armed)
Wants=network-online.target {worker_unit}
After=network-online.target {worker_unit}

{lease_note}

[Service]
Type=notify
NotifyAccess=main
WatchdogSec=30
TimeoutStartSec=180
User={engine_user}
Group=liquidity-migration
{state_dir_note}
StateDirectory={engine_state_dir_name}
WorkingDirectory={engine_state_dir}

{credential_note}
EnvironmentFile={credential_env}
UnsetEnvironment={engine_unset}
{unset_note}

{engine_env_note}
EnvironmentFile={engine_env}

ExecStart=/opt/liquidity-migration-engine/bin/engine run --config ${{ENGINE_CONFIG_FILE}}
# 143 is a clean SIGTERM stop.
SuccessExitStatus=143
Restart=always
RestartSec=5
KillMode=control-group
# The engine finishes its current write and drops its account lease on the way
# out; nothing here waits on a venue round trip.
TimeoutStopSec=30

# This process carries real orders; it does not yield CPU to the signal worker.
Nice=-5
IOSchedulingClass=best-effort
IOSchedulingPriority=4

NoNewPrivileges=true
PrivateTmp=true
ProtectProc=invisible
ProcSubset=pid
ProtectSystem=strict
ProtectHome=true
UMask=0027
ReadWritePaths=/run/lock/liquidity-migration {engine_state_dir} {spool_dir} {control_dir}
{memory_note}
MemoryMax=2G
LimitNOFILE=65536
TasksMax=256

[Install]
WantedBy=multi-user.target
"""

_WORKER_UNIT = """\
[Unit]
StartLimitIntervalSec=300
StartLimitBurst=5
Description=liquidity-migration credential-free Rust directional signal worker ({realm})
Wants=network-online.target
Before={engine_unit}
After=network-online.target

[Service]
Type=simple
User=liquidity-signal-worker
Group=liquidity-migration
StateDirectory={worker_state_dir_name}
StateDirectoryMode=0750
WorkingDirectory={worker_state_dir}
EnvironmentFile={worker_env}
UnsetEnvironment={worker_unset}
{venue_note}Environment=SIGNAL_WORKER_CONFIG_FILE={worker_config}
Environment=LONG_NATIVE_RULE_FILE=/opt/liquidity-migration/configs/long_native_v12.json
Environment=CARRY_SIGNAL_CONFIG_FILE=/opt/liquidity-migration/configs/lane2_carry_hold_v7.json
Environment=ENGINE_CONFIG_FILE={engine_config}
Environment=SIGNAL_WORKER_SPOOL_DIR={spool_dir}
Environment=SIGNAL_WORKER_STATE_DIR={worker_state_dir}
Environment=SIGNAL_WORKER_HEARTBEAT_FILE={worker_heartbeat}
ExecStart=/opt/liquidity-migration-engine/bin/signal-worker live \\
    --signal-config ${{SIGNAL_WORKER_CONFIG_FILE}} \\
    --long-rule ${{LONG_NATIVE_RULE_FILE}} \\
    --carry-config ${{CARRY_SIGNAL_CONFIG_FILE}} \\
    --operational-config ${{OPERATIONAL_PROFILE_FILE}} \\
    --engine-config ${{ENGINE_CONFIG_FILE}} \\
    --spool-dir ${{SIGNAL_WORKER_SPOOL_DIR}} \\
    --state-dir ${{SIGNAL_WORKER_STATE_DIR}} \\
    --heartbeat ${{SIGNAL_WORKER_HEARTBEAT_FILE}}
SuccessExitStatus=143
Restart=always
RestartSec=5
KillMode=control-group
TimeoutStopSec=60
Nice={worker_nice}
IOSchedulingClass=best-effort
IOSchedulingPriority=6
NoNewPrivileges=true
PrivateTmp=true
ProtectProc=invisible
ProcSubset=pid
ProtectSystem=strict
ProtectHome=true
UMask=0027
ReadWritePaths={worker_state_dir} {spool_dir}
MemoryMax=1024M
MemorySwapMax=384M
LimitNOFILE=65536
TasksMax=256

[Install]
WantedBy=multi-user.target
"""

_WORKER_VENUE_NOTE = """\
# Public market data comes from {venue_display}'s own public API (`sources.public_venue`
# in this realm's configs/signal-worker.<realm>.json); the account owner this
# worker feeds trades {venue_display}.
"""

_LIVENESS_SERVICE = """\
[Unit]
Description=liquidity-migration {label}Rust owner and signal-worker liveness watchdog
Wants=network-online.target
# No Requires= on the owner: the observer must still run when the owner is
# stopped or failed, which is exactly what it alerts on.
After=network-online.target

[Service]
Type=oneshot
KillMode=control-group
# The oneshot default is TimeoutStartSec=infinity, and an OnUnitActiveSec timer
# cannot re-trigger while its unit is activating, so one hung run would silence
# the watchdog forever. Bound stalled delivery as well as stalled detection.
TimeoutStartSec=120
User=liquidity-observer
Group=liquidity-migration
# Read-only journal access: a CRITICAL page carries the failing unit's last
# 40 journal lines to the on-call agent.
SupplementaryGroups=systemd-journal
WorkingDirectory=/opt/liquidity-migration
{observer_note}
EnvironmentFile=/etc/liquidity-migration/notifications.env
EnvironmentFile=/etc/liquidity-migration/oncall.env
UnsetEnvironment={liveness_unset}
Environment=TELEGRAM_ENABLED=1
Environment=PYTHONDONTWRITEBYTECODE=1
NoNewPrivileges=true
PrivateTmp=true
ProtectProc=invisible
ProtectSystem=strict
ProtectHome=true
InaccessiblePaths={inaccessible}
StateDirectory=liquidity-migration/liveness-{realm}
Environment=LIVENESS_STATE_FILE={liveness_state_file}
ReadWritePaths={liveness_state_dir}
MemoryMax=512M
# An observer must not preempt the Rust worker it observes (it runs Nice={worker_nice},
# the owners Nice=-5).
Nice=10
ExecStart=/opt/liquidity-migration/.venv/bin/python scripts/runtime/check_fleet_liveness.py \\
    --account-scope {realm} --cooldown-min 60 --require-oncall --engine-rates
SuccessExitStatus=0
StandardOutput=journal
StandardError=journal
"""

_LIVENESS_TIMER = """\
[Unit]
Description=Run the liquidity-migration {realm} watchdog every 30 seconds

[Timer]
OnActiveSec=10s
OnUnitActiveSec=30s
AccuracySec=1s
# Persistent applies only to calendar timers, not this monotonic schedule.

[Install]
WantedBy=timers.target
"""

_PRACTICE_LIVENESS_OBSERVER_NOTE = """\
# Detection reads the manifest and published artifacts. Delivery reads only
# dedicated route files; no venue, strategy, or owner configuration crosses
# into the watchdog."""


# ------------------------------------------------------------ env templates

_ENGINE_ENV_PRACTICE = """\
# {engine_env}  --  root-owned, mode 0600
#
# The Rust execution engine's unit settings. Deployment installs this whole
# template when the file is absent. On older hosts it atomically adds only the
# three missing identity bindings below, preserving every existing host dial;
# an existing empty or different binding is refused instead of overwritten.
#
{env_account_note}
#
# Format: strict KEY=value, one per line, no inline comments, no quotes, no
# shell expansion. systemd reads it verbatim.

# ---------------------------------------------------------------------------
# 1. The engine's config
# ---------------------------------------------------------------------------
# Ordered native strategies, account limits, WAL paths, and the compiled-in
# venue adapter. Rollout renders this file from the registered rules and the
# installed operational profile, then checks the exact bytes before activation.
ENGINE_CONFIG_FILE={engine_config}

# Required identity binding. Obtain the exact numeric/string user id from a
# read-only authenticated venue response; never infer it from an API-key label.
# Activation refuses a missing value or a heartbeat from another account.
EXPECTED_ENGINE_ACCOUNT_USER_ID={account_id}
EXPECTED_ENGINE_VENUE={venue}
EXPECTED_ENGINE_REALM={engine_realm}


# ---------------------------------------------------------------------------
# 2. Being watched
# ---------------------------------------------------------------------------
# The fleet's watchdog (check_fleet_liveness.py, every three minutes) reads the
# engine's heartbeat file and pages when it goes stale, cannot be read, or says
# the engine has stopped opening positions. Leave this line out and the
# watchdog says nothing about the engine at all.
#
# THIS MUST BE THE SAME PATH as `heartbeat_path` in the [engine] block of the
# config above, and nothing checks that for you: the engine reads its path from
# the config, the watchdog reads it from here. Two different paths means the
# engine writes a file nobody reads and the watchdog pages about a file nobody
# writes. If `heartbeat_path` is left out of the config the engine writes
# nothing, so leave this out too.
LIVENESS_ENGINE_HEARTBEAT_FILE={engine_heartbeat}

# ---------------------------------------------------------------------------
# 3. How much it says (optional)
# ---------------------------------------------------------------------------
# Unset means info. Everything the engine prints goes to the journal, which is
# capped at 500M by the deploy — a debug level on a busy market fills that in
# hours and pushes out the fleet's own logs.
RUST_LOG=info
"""

_ENGINE_ENV_FUNDED = """\
# {engine_env}  --  root-owned, mode 0600
#
{env_intro}
#
{env_credential_note}
#
# Format: strict KEY=value, one per line, no inline comments, no quotes, no
# shell expansion. systemd reads it verbatim.

# ---------------------------------------------------------------------------
# 1. The engine's config
# ---------------------------------------------------------------------------
{env_config_note}
ENGINE_CONFIG_FILE={engine_config}

{env_identity_note}
EXPECTED_ENGINE_ACCOUNT_USER_ID={account_id}
EXPECTED_ENGINE_VENUE={venue}
{env_realm_note}EXPECTED_ENGINE_REALM={engine_realm}
{env_section_gap}
# ---------------------------------------------------------------------------
# 2. Being watched
# ---------------------------------------------------------------------------
{env_watched_note}
LIVENESS_ENGINE_HEARTBEAT_FILE={engine_heartbeat}

# ---------------------------------------------------------------------------
# 3. How much it says (optional)
# ---------------------------------------------------------------------------
# Unset means info. Everything the engine prints goes to the journal, which is
# capped at 500M by the deploy — a debug level on a busy market fills that in
# hours and pushes out the fleet's own logs.
RUST_LOG=info
"""

_WORKER_ENV_TEMPLATE = """\
# {worker_source_env} -- root-owned, 0600
#
{worker_env_note}

SIGNAL_WORKER_REALM={realm}
OPERATIONAL_PROFILE_FILE={worker_profile}
"""

_WORKER_ENV_NOTE_PRACTICE = "# Reviewed public inputs copied into the unprivileged Rust signal worker."


# --------------------------------------------------------- manifest regions

#: Each generated manifest region: its marker suffix and the row it renders per
#: realm. Four regions, because two hand-written realm extras (the mainnet
#: execution study, the demo chaos drill) sit between the clusters.
GENERATED_MANIFEST_REGIONS = (
    "REALM LIVENESS TIMERS",
    "REALM SIGNAL WORKERS",
    "REALM LIVENESS SERVICES",
    "REALM OWNERS",
)

_MARKER_SOURCE = "liquidity_migration.policy.realms"


def _region_markers(region: str) -> tuple[str, str]:
    return (
        f"# BEGIN GENERATED {region} -- {_MARKER_SOURCE}",
        f"# END GENERATED {region}",
    )


def _manifest_row(row: Realm, region: str) -> str:
    if region == "REALM OWNERS":
        return "|".join(
            (
                row.engine_unit,
                "service",
                row.realm,
                "owner",
                str(row.owner_stop),
                row.activation,
                row.operator,
                "-",
                "active",
                row.engine_heartbeat,
                *("-",) * 6,
            )
        )
    if region == "REALM SIGNAL WORKERS":
        return "|".join(
            (
                row.worker_unit,
                "service",
                row.realm,
                "downstream",
                str(row.worker_stop),
                row.activation,
                row.operator,
                "-",
                "active",
                row.worker_heartbeat,
                *("-",) * 6,
            )
        )
    if region == "REALM LIVENESS TIMERS":
        return "|".join(
            (
                row.liveness_timer,
                "timer",
                row.realm,
                "downstream",
                str(row.liveness_timer_stop),
                row.activation,
                row.operator,
                row.liveness_service,
                "timer",
                "-",
                row.liveness_service,
                "10",
                "30",
                "1",
                "120",
                "-",
            )
        )
    if region == "REALM LIVENESS SERVICES":
        return "|".join(
            (
                row.liveness_service,
                "service",
                row.realm,
                "downstream",
                str(row.liveness_service_stop),
                "job-now",
                row.operator,
                row.engine_unit,
                "none",
                *("-",) * 7,
            )
        )
    raise KeyError(region)


def render_fleet_manifest(current: str, rows: tuple[Realm, ...]) -> str:
    """Replace every generated region of the manifest, leaving the rest alone."""

    lines = current.splitlines()
    for region in GENERATED_MANIFEST_REGIONS:
        begin, end = _region_markers(region)
        try:
            first = lines.index(begin)
            last = lines.index(end)
        except ValueError as error:
            raise ValueError(f"fleet manifest has no {region} region") from error
        if last <= first:
            raise ValueError(f"fleet manifest {region} region is inverted")
        body = [_manifest_row(row, region) for row in rows]
        lines[first + 1 : last] = body
    return "\n".join(lines) + "\n"


# ----------------------------------------------------------------- renderer


def _venue_fact(row: Realm, key: str) -> str:
    facts = VENUE_FACTS.get(row.venue, {})
    if key in facts:
        return str(facts[key])
    if key not in _DEFAULT_VENUE_FACTS:
        raise KeyError(f"venue {row.venue} declares no {key}")
    return _DEFAULT_VENUE_FACTS[key].format(
        display=row.venue_display,
        realm=row.realm,
        credential_env=row.credential_env,
        engine_env_template=row.engine_env_template,
    )


def _realm_prose(row: Realm, key: str, default: str) -> str:
    return REALM_PROSE.get(row.realm, {}).get(key, default)


def _engine_unit_text(row: Realm) -> str:
    if not row.funded:
        return _ENGINE_UNIT_PRACTICE.format(
            realm=row.realm,
            worker_unit=row.worker_unit,
            engine_user=row.engine_user,
            engine_state_dir_name=row.engine_state_dir_name,
            engine_state_dir=row.engine_state_dir,
            account_note=_realm_prose(
                row,
                "engine_account_note",
                f"# The fleet's {row.realm} account. The engine is its only writer and holds\n"
                "# its single-writer lease.",
            ),
            credential_env=row.credential_env,
            engine_unset=" ".join(row.engine_unset),
            engine_env_template=row.engine_env_template,
            engine_env=row.engine_env,
            spool_dir=row.spool_dir,
            control_dir=row.control_dir,
            memory_note=_venue_fact(row, "engine_memory_note"),
        )
    return _ENGINE_UNIT_FUNDED.format(
        account_phrase=_venue_fact(row, "engine_account_phrase"),
        worker_unit=row.worker_unit,
        lease_note=_venue_fact(row, "engine_lease_note"),
        engine_user=row.engine_user,
        state_dir_note=_venue_fact(row, "engine_state_dir_note"),
        engine_state_dir_name=row.engine_state_dir_name,
        engine_state_dir=row.engine_state_dir,
        credential_note=_venue_fact(row, "engine_credential_note"),
        credential_env=row.credential_env,
        engine_unset=" ".join(row.engine_unset),
        unset_note=_venue_fact(row, "engine_unset_note"),
        engine_env_note=_venue_fact(row, "engine_env_note"),
        engine_env=row.engine_env,
        spool_dir=row.spool_dir,
        control_dir=row.control_dir,
        memory_note=_venue_fact(row, "engine_memory_note"),
    )


def _worker_unit_text(row: Realm) -> str:
    venue_note = ""
    if row.venue != PUBLIC_DATA_VENUE:
        venue_note = _WORKER_VENUE_NOTE.format(venue_display=row.venue_display)
    return _WORKER_UNIT.format(
        realm=row.realm,
        engine_unit=row.engine_unit,
        worker_state_dir_name=row.worker_state_dir_name,
        worker_state_dir=row.worker_state_dir,
        worker_env=row.worker_env,
        worker_unset=" ".join(row.worker_unset),
        venue_note=venue_note,
        worker_config=row.worker_config,
        engine_config=row.engine_config,
        spool_dir=row.spool_dir,
        worker_heartbeat=row.worker_heartbeat,
        worker_nice=_worker_nice(row),
    )


def _worker_nice(row: Realm) -> str:
    """The worker yields to a funded owner harder than to the practice one."""
    return "3" if row.funded else "5"


def _liveness_service_text(row: Realm, rows: tuple[Realm, ...]) -> str:
    label = f"{row.liveness_label} " if row.liveness_label else ""
    inaccessible = [
        "-/etc/liquidity-migration/notifications.env",
        "-/etc/liquidity-migration/oncall.env",
    ]
    scope = set(row.liveness_scope)
    for other in rows:
        if other.venue in scope:
            inaccessible.append(f"-{other.credential_env}")
    observer_note = (
        _venue_fact(row, "liveness_observer_note")
        if row.funded
        else _PRACTICE_LIVENESS_OBSERVER_NOTE
    )
    return _LIVENESS_SERVICE.format(
        label=label,
        observer_note=observer_note,
        liveness_unset=" ".join(row.liveness_unset),
        inaccessible=" ".join(inaccessible),
        realm=row.realm,
        liveness_state_file=row.liveness_state_file,
        liveness_state_dir=row.liveness_state_dir,
        worker_nice=_worker_nice(row),
    )


def _engine_env_template_text(row: Realm) -> str:
    if not row.funded:
        return _ENGINE_ENV_PRACTICE.format(
            engine_env=row.engine_env,
            env_account_note=_realm_prose(
                row,
                "env_account_note",
                "# Its credentials remain in the separate file below and are never copied\n"
                "# into this non-secret projection.",
            ),
            engine_config=row.engine_config,
            account_id=_realm_prose(row, "env_account_id", ""),
            venue=row.venue,
            engine_realm=row.engine_realm,
            engine_heartbeat=row.engine_heartbeat,
        )
    return _ENGINE_ENV_FUNDED.format(
        engine_env=row.engine_env,
        env_intro=_venue_fact(row, "env_intro"),
        env_credential_note=_venue_fact(row, "env_credential_note"),
        env_config_note=_venue_fact(row, "env_config_note"),
        engine_config=row.engine_config,
        env_identity_note=_venue_fact(row, "env_identity_note"),
        account_id=_venue_fact(row, "account_id_placeholder"),
        venue=row.venue,
        env_realm_note=_venue_fact(row, "env_realm_note"),
        engine_realm=row.engine_realm,
        env_section_gap=_venue_fact(row, "env_section_gap"),
        env_watched_note=_venue_fact(row, "env_watched_note"),
        engine_heartbeat=row.engine_heartbeat,
    )


def _worker_env_template_text(row: Realm) -> str:
    note = (
        _venue_fact(row, "worker_env_note")
        if row.funded
        else _WORKER_ENV_NOTE_PRACTICE
    )
    return _WORKER_ENV_TEMPLATE.format(
        worker_source_env=row.worker_source_env,
        worker_env_note=note,
        realm=row.realm,
        worker_profile=row.worker_profile,
    )


def render_realm_files(root: Path | None = None) -> dict[Path, bytes]:
    """Every file the realm table generates, keyed by path under `root`."""

    base = _REPO_ROOT if root is None else root
    rows = realms(base)
    out: dict[Path, bytes] = {}
    units = base / "deploy" / "systemd"
    for row in rows:
        out[units / row.engine_unit] = _engine_unit_text(row).encode("utf-8")
        out[units / row.worker_unit] = _worker_unit_text(row).encode("utf-8")
        out[units / row.liveness_service] = _liveness_service_text(row, rows).encode("utf-8")
        out[units / row.liveness_timer] = _LIVENESS_TIMER.format(realm=row.realm).encode("utf-8")
        out[base / row.engine_env_template] = _engine_env_template_text(row).encode("utf-8")
        out[base / row.worker_env_template] = _worker_env_template_text(row).encode("utf-8")
    manifest = base / "deploy" / "fleet_manifest.tsv"
    out[manifest] = render_fleet_manifest(
        manifest.read_text(encoding="utf-8"), rows
    ).encode("utf-8")
    return out


def _drift(root: Path) -> list[Path]:
    return [
        path
        for path, body in sorted(render_realm_files(root).items())
        if not path.is_file() or path.read_bytes() != body
    ]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("render", "check"))
    parser.add_argument(
        "--root", default=str(_REPO_ROOT), help="repository root (default: this checkout)"
    )
    args = parser.parse_args(argv)
    root = Path(args.root)
    try:
        rendered = render_realm_files(root)
    except (OSError, ValueError, KeyError) as error:
        print(f"realm render failed: {error}", file=sys.stderr)
        return 2
    if args.command == "check":
        drifted = [
            path
            for path, body in sorted(rendered.items())
            if not path.is_file() or path.read_bytes() != body
        ]
        for path in drifted:
            print(path.relative_to(root))
        if drifted:
            print(
                f"{len(drifted)} generated file(s) differ from the realm table; "
                "run python -m liquidity_migration.policy.realms render",
                file=sys.stderr,
            )
            return 1
        print(f"{len(rendered)} generated file(s) match deploy/realms.tsv")
        return 0
    written = 0
    for path, body in sorted(rendered.items()):
        if path.is_file() and path.read_bytes() == body:
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)
        written += 1
        print(path.relative_to(root))
    print(f"{written} generated file(s) written, {len(rendered) - written} unchanged")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
