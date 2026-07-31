"""The owner edits one file, runs one command, and is told what is left.

Ergonomics: a dial in the env file must reach the rendered profile, and the committed
profile must be the render of the committed defaults, or the two drift apart.

Safety: no dial combination may produce an envelope the load-time proof would reject,
the preflight must never print a secret, and nothing in this path may set
``REAL_MONEY``, write a credential, or start a unit.
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from liquidity_migration.operational_profile import load_operational_profile_bytes
from liquidity_migration.real_money_arming import preflight
from liquidity_migration.real_money_profile import (
    RealMoneyDials,
    dial_environment_keys,
    parse_real_money_dials,
    render_real_money_profile,
)

REPO = Path(__file__).resolve().parents[1]
TEMPLATE = REPO / "deploy" / "bybit-mainnet.env.template"
OWNER_TEMPLATE = REPO / "deploy" / "account-execution-mainnet.env.template"


def _env_file(tmp_path: Path, name: str, body: str) -> Path:
    path = tmp_path / name
    path.write_text(body, encoding="utf-8")
    path.chmod(0o600)
    return path


def _filled_credential_env(tmp_path: Path, **overrides: str) -> Path:
    body = TEMPLATE.read_text(encoding="utf-8")
    replacements = {
        "BYBIT_REAL_API_KEY": "placeholder-not-a-real-key",
        "BYBIT_REAL_API_SECRET": "placeholder-not-a-real-secret",
        **overrides,
    }
    lines = []
    for line in body.splitlines():
        key = line.split("=", 1)[0]
        if key in replacements:
            lines.append(f"{key}={replacements[key]}")
        else:
            lines.append(line)
    return _env_file(tmp_path, "bybit-mainnet.env", "\n".join(lines) + "\n")


# --------------------------------------------------------------------------
# The committed profile IS the render of the committed defaults
# --------------------------------------------------------------------------


def test_the_committed_mainnet_profile_is_the_render_of_the_defaults() -> None:
    """Otherwise the file and the renderer are two answers to one question."""

    data, _profile = render_real_money_profile()
    assert data == (REPO / "configs" / "operational.mainnet.json").read_bytes()


def test_the_template_names_every_dial_the_renderer_reads() -> None:
    """A dial the template omits is one the owner cannot discover."""

    body = TEMPLATE.read_text(encoding="utf-8")
    for key in dial_environment_keys():
        assert f"{key}=" in body, key


def test_the_template_parses_as_a_strict_systemd_environment_file() -> None:
    from liquidity_migration.systemd_environment import parse_systemd_environment_bytes

    for template in (TEMPLATE, OWNER_TEMPLATE):
        values = parse_systemd_environment_bytes(template.read_bytes(), label=str(template))
        assert values, template

    credential = parse_systemd_environment_bytes(TEMPLATE.read_bytes(), label="template")
    # Shipped disarmed and empty. A template that arrived armed, or with a
    # credential in it, would be a template that arms by being copied.
    assert credential["REAL_MONEY"] == "false"
    assert credential["BYBIT_REAL_API_KEY"] == ""
    assert credential["BYBIT_REAL_API_SECRET"] == ""


# --------------------------------------------------------------------------
# Dials reach the profile, and cannot reach it unproven
# --------------------------------------------------------------------------


def test_a_dial_in_the_env_file_reaches_the_rendered_profile() -> None:
    dials = parse_real_money_dials(
        {
            "RM_MAX_LEVERAGE": "2.0",
            "RM_ENTRY_LEVERAGE": "1.6",
            "RM_ACCOUNT_GROSS_MULTIPLE": "1.6",
            "RM_CARRY_GROSS_SHARE": "0.5",
            "RM_CARRY_NOTIONAL_MULTIPLIER": "0.7",
            "RM_LONG_NOTIONAL_MULTIPLIER": "0.3",
        }
    )
    assert dials.max_leverage == 2.0
    _data, profile = render_real_money_profile(dials)
    assert profile.account_risk.max_leverage == 2.0
    assert profile.carry.entry_leverage == 1.6
    assert profile.account_risk.max_account_gross_notional_usdt == pytest.approx(
        1.6 * profile.capital_reference_usdt
    )
    carry = next(
        limit for limit in profile.account_risk.sleeve_limits if limit.sleeve == "carry"
    )
    assert carry.max_gross_notional_usdt == pytest.approx(
        0.5 * profile.account_risk.max_account_gross_notional_usdt
    )


def test_lowering_leverage_without_lowering_gross_names_the_dial_to_move() -> None:
    """A sleeve-level message would name a sleeve, not the dial at fault."""

    with pytest.raises(
        ValueError, match="RM_ACCOUNT_GROSS_MULTIPLE .* cannot exceed RM_ENTRY_LEVERAGE"
    ):
        render_real_money_profile(RealMoneyDials(entry_leverage=1.5))
    # Lowering gross to match is what the message asks for. The producers have
    # to come down with it -- less gross is less book -- and the proof says so
    # by naming the sleeve that no longer fits.
    _data, profile = render_real_money_profile(
        RealMoneyDials(
            entry_leverage=1.5,
            account_gross_multiple=1.5,
            carry_notional_multiplier=0.7,
            long_notional_multiplier=0.3,
        )
    )
    assert profile.account_risk.max_leverage == 2.0


#: (gross multiple, entry leverage, carry multiplier, long multiplier). Each is
#: a coherent deployment: producers sized to fit the account they are given.
_COHERENT_DIALS = (
    (1.0, 1.0, 0.5, 0.2),
    (1.0, 2.0, 0.5, 0.2),
    (1.5, 1.5, 0.7, 0.3),
    (1.8, 1.8, 0.9, 0.35),
    (2.0, 2.0, 1.0, 0.4),
)


@pytest.mark.parametrize(("multiple", "leverage", "carry", "long"), _COHERENT_DIALS)
def test_the_partition_sums_inside_both_account_caps_at_any_leverage(
    multiple: float, leverage: float, carry: float, long: float
) -> None:
    """Deriving the margin share from leverage silently broke this below 2x."""

    _data, profile = render_real_money_profile(
        RealMoneyDials(
            max_leverage=max(leverage, 2.0),
            entry_leverage=leverage,
            account_gross_multiple=multiple,
            carry_notional_multiplier=carry,
            long_notional_multiplier=long,
        )
    )
    risk = profile.account_risk
    assert sum(limit.max_gross_notional_usdt for limit in risk.sleeve_limits) <= (
        risk.max_account_gross_notional_usdt + 1e-9
    )
    assert sum(limit.max_initial_margin_usdt for limit in risk.sleeve_limits) <= (
        risk.max_initial_margin_usdt + 1e-9
    )


def test_a_mistyped_dial_is_an_error_not_a_silent_default() -> None:
    """An operator who typed it meant to change it."""

    with pytest.raises(ValueError, match="RM_MAX_LEVERAGE must be numeric"):
        parse_real_money_dials({"RM_MAX_LEVERAGE": "2x"})
    with pytest.raises(ValueError, match="RM_MAX_LEVERAGE is present but empty"):
        parse_real_money_dials({"RM_MAX_LEVERAGE": "  "})
    with pytest.raises(ValueError, match="must be an integer"):
        parse_real_money_dials({"RM_CARRY_MAX_NEW_ENTRIES_PER_CYCLE": "10.5"})


@pytest.mark.parametrize(
    ("dials", "message"),
    [
        ({"equity_fraction": 1.5}, "RM_EQUITY_FRACTION cannot exceed 1"),
        ({"entry_leverage": 5.0}, "RM_ENTRY_LEVERAGE cannot exceed RM_MAX_LEVERAGE"),
        ({"initial_margin_fraction": 2.0}, "RM_INITIAL_MARGIN_FRACTION cannot exceed 1"),
        ({"account_gross_multiple": 9.0}, "RM_ACCOUNT_GROSS_MULTIPLE cannot exceed"),
        ({"carry_gross_share": 0.9}, "must leave room for the"),
        ({"carry_stop_loss_fraction": 1.0}, "must sit in \\(0, 1\\)"),
        ({"max_leverage": 0.0}, "RM_MAX_LEVERAGE must be finite and positive"),
        ({"max_leverage": 5.0}, "RM_MAX_LEVERAGE cannot exceed 2"),
        ({"carry_max_new_entries_per_cycle": 0}, "must be a positive integer"),
    ],
)
def test_no_dial_set_can_produce_an_envelope_the_proof_would_reject(
    dials: dict, message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        render_real_money_profile(RealMoneyDials(**dials))


def test_a_producer_dialled_outside_its_partition_share_is_refused() -> None:
    """The load-time proof is what catches this, and it runs at render time."""

    with pytest.raises(ValueError, match="'long' gross envelope exceeds its sleeve_limits"):
        render_real_money_profile(RealMoneyDials(long_notional_multiplier=0.5))


def test_every_render_is_reloadable_and_proved() -> None:
    for dials in (
        RealMoneyDials(),
        # A bigger CARRY share and a smaller LONG one, with LONG sized to fit it.
        RealMoneyDials(
            carry_gross_share=0.7, long_gross_share=0.2, long_notional_multiplier=0.2
        ),
        RealMoneyDials(entry_leverage=1.8, account_gross_multiple=1.8,
                       carry_notional_multiplier=0.9, long_notional_multiplier=0.35),
        RealMoneyDials(equity_fraction=0.25, long_notional_multiplier=0.1),
        RealMoneyDials(daily_loss_fraction=0.02, expand_dead_band_fraction=0.0),
        RealMoneyDials(equity_floor_usdt=1.0, carry_max_new_entries_per_cycle=1),
    ):
        data, profile = render_real_money_profile(dials)
        assert load_operational_profile_bytes(data).source_sha256 == profile.source_sha256


# --------------------------------------------------------------------------
# Preflight
# --------------------------------------------------------------------------


def test_preflight_reports_the_missing_files_rather_than_raising(tmp_path: Path) -> None:
    results = preflight(
        credential_env=tmp_path / "absent-a.env",
        owner_env=tmp_path / "absent-b.env",
        check_sleeves=False,
    )
    assert [row.ok for row in results] == [False, False]
    assert all("does not exist" in row.detail for row in results)
    assert all(row.fix for row in results)


def test_preflight_never_puts_a_secret_in_its_report(tmp_path: Path) -> None:
    """The whole report is rendered to a terminal and often pasted into chat."""

    secret = "SUPER-SECRET-VALUE-0123456789"
    credential = _filled_credential_env(
        tmp_path, BYBIT_REAL_API_KEY=secret, BYBIT_REAL_API_SECRET=secret
    )
    owner = _env_file(tmp_path, "owner.env", OWNER_TEMPLATE.read_text(encoding="utf-8"))
    results = preflight(credential_env=credential, owner_env=owner, check_sleeves=False)

    rendered = "\n".join(row.render() for row in results)
    assert secret not in rendered
    assert "BYBIT_REAL_API_KEY" in rendered  # reported by name, never by value
    key_row = next(row for row in results if row.name == "BYBIT_REAL_API_KEY")
    assert key_row.ok and key_row.detail == "set"


def test_preflight_reports_the_arming_switch_as_the_owners_act(tmp_path: Path) -> None:
    credential = _filled_credential_env(tmp_path)
    owner = _env_file(tmp_path, "owner.env", OWNER_TEMPLATE.read_text(encoding="utf-8"))

    results = preflight(credential_env=credential, owner_env=owner, check_sleeves=False)
    real_money = next(row for row in results if row.name == "REAL_MONEY")
    assert not real_money.ok
    assert "by hand" in real_money.fix

    armed = _filled_credential_env(tmp_path, REAL_MONEY="true")
    results = preflight(credential_env=armed, owner_env=owner, check_sleeves=False)
    assert next(row for row in results if row.name == "REAL_MONEY").ok


def test_preflight_refuses_a_demo_key_in_the_mainnet_file(tmp_path: Path) -> None:
    body = TEMPLATE.read_text(encoding="utf-8") + "\nBYBIT_DEMO_API_KEY=leftover\n"
    credential = _env_file(tmp_path, "bybit-mainnet.env", body)
    owner = _env_file(tmp_path, "owner.env", OWNER_TEMPLATE.read_text(encoding="utf-8"))

    results = preflight(credential_env=credential, owner_env=owner, check_sleeves=False)
    demo = next(row for row in results if row.name == "BYBIT_DEMO_API_KEY")
    assert not demo.ok
    assert "never reach a funded run" in demo.fix


def test_preflight_reports_a_bad_dial_without_crashing(tmp_path: Path) -> None:
    body = TEMPLATE.read_text(encoding="utf-8").replace(
        "RM_ENTRY_LEVERAGE=2.0", "RM_ENTRY_LEVERAGE=50.0"
    )
    credential = _env_file(tmp_path, "bybit-mainnet.env", body)
    owner = _env_file(tmp_path, "owner.env", OWNER_TEMPLATE.read_text(encoding="utf-8"))

    results = preflight(credential_env=credential, owner_env=owner, check_sleeves=False)
    dials = next(row for row in results if row.name == "dials")
    assert not dials.ok
    assert "RM_ENTRY_LEVERAGE cannot exceed" in dials.detail


def test_preflight_refuses_a_world_readable_credential_file(tmp_path: Path) -> None:
    credential = _filled_credential_env(tmp_path)
    credential.chmod(0o644)
    owner = _env_file(tmp_path, "owner.env", OWNER_TEMPLATE.read_text(encoding="utf-8"))

    results = preflight(credential_env=credential, owner_env=owner, check_sleeves=False)
    assert not results[0].ok
    assert "must be root-owned 0600" in results[0].detail


def test_preflight_writes_nothing(tmp_path: Path) -> None:
    credential = _filled_credential_env(tmp_path)
    owner = _env_file(tmp_path, "owner.env", OWNER_TEMPLATE.read_text(encoding="utf-8"))
    before = {
        path: (path.read_bytes(), stat.S_IMODE(path.stat().st_mode))
        for path in sorted(tmp_path.iterdir())
    }

    preflight(credential_env=credential, owner_env=owner, check_sleeves=False)

    after = {
        path: (path.read_bytes(), stat.S_IMODE(path.stat().st_mode))
        for path in sorted(tmp_path.iterdir())
    }
    assert after == before


def test_the_arming_module_never_sets_real_money_or_starts_a_unit() -> None:
    """It may *say* REAL_MONEY=true in a hint; it may not *do* it."""

    import ast

    path = REPO / "liquidity_migration" / "real_money_arming.py"
    source = path.read_text(encoding="utf-8")
    assert "systemctl" not in source
    assert "subprocess" not in source
    tree = ast.parse(source)
    for node in ast.walk(tree):
        # os.environ[...] = ... and os.environ.update(...) are the two ways a
        # tool could arm the process it is only supposed to inspect.
        rendered = ast.unparse(node) if isinstance(node, (ast.Subscript, ast.Call)) else ""
        assert not rendered.startswith("os.environ"), rendered


def test_render_profile_writes_one_private_artifact(tmp_path: Path) -> None:
    from liquidity_migration.real_money_arming import main

    credential = _filled_credential_env(tmp_path)
    output = tmp_path / "risk-policy.json"
    code = main(
        [
            "render-profile",
            "--from-env",
            str(credential),
            "--execute",
            "--output",
            str(output),
        ]
    )
    assert code == 0
    assert stat.S_IMODE(output.stat().st_mode) == 0o600
    profile = load_operational_profile_bytes(output.read_bytes())
    assert profile.account_risk.sleeve_limits
    # It reads the dials out of the credential file and writes none of it back.
    assert "BYBIT" not in output.read_text(encoding="utf-8")


def test_render_profile_refuses_to_clobber_without_being_told(tmp_path: Path) -> None:
    from liquidity_migration.real_money_arming import main

    credential = _filled_credential_env(tmp_path)
    output = tmp_path / "risk-policy.json"
    output.write_text("{}", encoding="utf-8")
    args = ["render-profile", "--from-env", str(credential), "--execute", "--output", str(output)]
    assert main(args) == 2
    assert output.read_text(encoding="utf-8") == "{}"
    assert main([*args, "--overwrite"]) == 0
    assert json.loads(output.read_text(encoding="utf-8"))["kind"]


def test_render_profile_is_a_dry_run_without_execute(tmp_path: Path, capsys) -> None:
    from liquidity_migration.real_money_arming import main

    credential = _filled_credential_env(tmp_path)
    assert main(["render-profile", "--from-env", str(credential)]) == 0
    printed = capsys.readouterr().out
    assert json.loads(printed)["kind"] == "liquidity_migration_operational_profile"
    assert not (tmp_path / "risk-policy.json").exists()


def test_the_owner_template_declares_disjoint_mainnet_roots() -> None:
    from liquidity_migration.systemd_environment import parse_systemd_environment_bytes

    values = parse_systemd_environment_bytes(OWNER_TEMPLATE.read_bytes(), label="owner")
    roots = [
        values["ACCOUNT_EXECUTION_ROOT"],
        values["ACCOUNT_INTENT_INBOX_ROOT"],
        values["ACCOUNT_CAPTURE_ROOT"],
    ]
    assert len(set(roots)) == len(roots)
    assert all("mainnet" in root for root in roots)
    assert values["ACCOUNT_VENUE_REALM"] == "mainnet"
    # The frozen universe and the owner's symbol list must be the same file.
    assert values["CANDIDATE_UNIVERSE_FILE"] == values["ACCOUNT_SYMBOLS_FILE"]
    # Disjoint from the demo roots, which live under different names entirely.
    demo_env = (REPO / "deploy" / "systemd" / "liquidity-migration-account-execution.service").read_text()
    for root in roots:
        assert root not in demo_env


def test_the_scratch_environment_is_untouched_by_the_module_import() -> None:
    """Importing an arming tool must not arm anything."""

    assert os.environ.get("REAL_MONEY") is None


def test_preflight_catches_an_empty_telegram_pair_before_arming(tmp_path: Path) -> None:
    """The owner unit enables Telegram; an empty token is a start-up failure."""

    body = TEMPLATE.read_text(encoding="utf-8")
    credential = _env_file(tmp_path, "bybit-mainnet.env", body)
    owner = _env_file(tmp_path, "owner.env", OWNER_TEMPLATE.read_text(encoding="utf-8"))

    results = preflight(credential_env=credential, owner_env=owner, check_sleeves=False)
    row = next(check for check in results if check.name == "notifications")
    assert not row.ok
    assert "TELEGRAM_BOT_TOKEN" in row.detail

    filled = _env_file(
        tmp_path,
        "bybit-mainnet-2.env",
        body.replace("TELEGRAM_BOT_TOKEN=", "TELEGRAM_BOT_TOKEN=x").replace(
            "TELEGRAM_CHAT_ID=", "TELEGRAM_CHAT_ID=1"
        ),
    )
    results = preflight(credential_env=filled, owner_env=owner, check_sleeves=False)
    assert next(check for check in results if check.name == "notifications").ok


def test_preflight_catches_a_profile_that_is_not_the_render_of_the_dials(
    tmp_path: Path,
) -> None:
    """The preflight must check that the installed profile IS the render of the dials.
    Printing the dial-derived envelope as a PASS row while only checking that the
    profile exists produces a green report describing an envelope nothing enforces.
    """

    from liquidity_migration.real_money_arming import main

    installed = tmp_path / "risk-policy.json"
    stale = _filled_credential_env(
        tmp_path,
        **{"RM_CARRY_GROSS_SHARE": "0.70", "RM_LONG_GROSS_SHARE": "0.20",
           "RM_LONG_NOTIONAL_MULTIPLIER": "0.2"},
    )
    assert main(["render-profile", "--from-env", str(stale), "--execute",
                 "--output", str(installed)]) == 0

    owner = _env_file(
        tmp_path,
        "owner.env",
        OWNER_TEMPLATE.read_text(encoding="utf-8").replace(
            "ACCOUNT_RISK_POLICY_FILE=/etc/liquidity-migration/account-execution-mainnet/risk-policy.json",
            f"ACCOUNT_RISK_POLICY_FILE={installed}",
        ),
    )
    current_dir = tmp_path / "current"
    current_dir.mkdir()
    current = _filled_credential_env(current_dir)

    results = preflight(credential_env=current, owner_env=owner, check_sleeves=False)
    drift = next(row for row in results if row.name == "profile matches dials")
    assert not drift.ok
    assert "is not the one that would be enforced" in drift.detail

    # Re-rendering from the current dials clears it.
    assert main(["render-profile", "--from-env", str(current), "--execute",
                 "--output", str(installed), "--overwrite"]) == 0
    results = preflight(credential_env=current, owner_env=owner, check_sleeves=False)
    assert next(row for row in results if row.name == "profile matches dials").ok


def test_preflight_checks_the_state_roots_exist(tmp_path: Path) -> None:
    """Issuance hard-requires them and nothing in the repository creates them."""

    credential = _filled_credential_env(tmp_path)
    roots = tmp_path / "state"
    owner_body = OWNER_TEMPLATE.read_text(encoding="utf-8")
    for key, name in (
        ("ACCOUNT_EXECUTION_ROOT", "account"),
        ("ACCOUNT_INTENT_INBOX_ROOT", "inbox"),
        ("ACCOUNT_CAPTURE_ROOT", "capture"),
    ):
        line = next(ln for ln in owner_body.splitlines() if ln.startswith(f"{key}="))
        owner_body = owner_body.replace(line, f"{key}={roots / name}")
    owner = _env_file(tmp_path, "owner.env", owner_body)

    results = preflight(credential_env=credential, owner_env=owner, check_sleeves=False)
    row = next(check for check in results if check.name == "state roots")
    assert not row.ok
    assert "create-state-roots" in row.fix

    for name in ("account", "inbox", "capture"):
        (roots / name).mkdir(parents=True)
    results = preflight(credential_env=credential, owner_env=owner, check_sleeves=False)
    assert next(check for check in results if check.name == "state roots").ok


# --------------------------------------------------------------------------
# create-state-roots
# --------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _no_installed_realm_envs(tmp_path: Path, monkeypatch) -> None:
    """Point the other-realm env files at absent paths so a VPS run stays hermetic."""

    from liquidity_migration import real_money_arming

    monkeypatch.setattr(real_money_arming, "DEMO_OWNER_ENV", tmp_path / "absent-demo.env")
    monkeypatch.setattr(real_money_arming, "PAPER_OWNER_ENV", tmp_path / "absent-paper.env")


def _owner_env_with_roots(tmp_path: Path, roots: Path, **overrides: str) -> Path:
    body = OWNER_TEMPLATE.read_text(encoding="utf-8")
    replacements = {
        "ACCOUNT_EXECUTION_ROOT": str(roots / "account"),
        "ACCOUNT_INTENT_INBOX_ROOT": str(roots / "inbox"),
        "ACCOUNT_CAPTURE_ROOT": str(roots / "capture"),
        "STRATEGY_TARGET_CAPTURE_PATH": str(roots / "capture" / "strategy-targets.jsonl"),
        **overrides,
    }
    for key, value in replacements.items():
        line = next(ln for ln in body.splitlines() if ln.startswith(f"{key}="))
        body = body.replace(line, f"{key}={value}")
    return _env_file(tmp_path, "owner.env", body)


def test_create_state_roots_dry_run_creates_nothing(tmp_path: Path, capsys) -> None:
    from liquidity_migration.real_money_arming import main

    roots = tmp_path / "state"
    owner = _owner_env_with_roots(tmp_path, roots)

    assert main(["create-state-roots", "--owner-env", str(owner)]) == 0
    printed = capsys.readouterr()
    assert not roots.exists()
    for name in ("account", "inbox", "capture"):
        assert str(roots / name) in printed.out
    assert "dry run" in printed.err


def test_create_state_roots_creates_all_three_private(tmp_path: Path, capsys) -> None:
    from liquidity_migration.real_money_arming import main

    roots = tmp_path / "state"
    owner = _owner_env_with_roots(tmp_path, roots)

    assert main(["create-state-roots", "--owner-env", str(owner), "--execute"]) == 0
    summary = json.loads(capsys.readouterr().out)
    created = {roots / name for name in ("account", "inbox", "capture")}
    assert {Path(path) for path in summary["created"]} == created
    assert summary["already_present"] == []
    for path in created:
        assert path.is_dir()
        assert stat.S_IMODE(path.stat().st_mode) == 0o700
        assert summary["roots"][str(path)]["mode"] == "0700"
        assert summary["roots"][str(path)]["uid"] == os.getuid()
    # The capture path is a file, not a directory: only its parent is created.
    assert not (roots / "capture" / "strategy-targets.jsonl").exists()


def test_create_state_roots_creates_the_capture_paths_own_parent(
    tmp_path: Path, capsys
) -> None:
    """The shipped template nests it in the capture root; a moved one still works."""

    from liquidity_migration.real_money_arming import main

    roots = tmp_path / "state"
    targets = roots / "targets"
    owner = _owner_env_with_roots(
        tmp_path,
        roots,
        STRATEGY_TARGET_CAPTURE_PATH=str(targets / "strategy-targets.jsonl"),
    )

    assert main(["create-state-roots", "--owner-env", str(owner), "--execute"]) == 0
    summary = json.loads(capsys.readouterr().out)
    assert str(targets) in summary["created"]
    assert targets.is_dir()
    assert not (targets / "strategy-targets.jsonl").exists()


def test_create_state_roots_handles_one_root_nested_in_another(
    tmp_path: Path, capsys
) -> None:
    """Creating the child materialises the parent; both are still declared roots."""

    from liquidity_migration.real_money_arming import main

    roots = tmp_path / "state"
    owner = _owner_env_with_roots(
        tmp_path,
        roots,
        ACCOUNT_EXECUTION_ROOT=str(roots / "inbox" / "account"),
        ACCOUNT_INTENT_INBOX_ROOT=str(roots / "inbox"),
    )

    assert main(["create-state-roots", "--owner-env", str(owner), "--execute"]) == 0
    summary = json.loads(capsys.readouterr().out)
    assert len(summary["created"]) == 3
    for path in (roots / "inbox", roots / "inbox" / "account"):
        assert stat.S_IMODE(path.stat().st_mode) == 0o700
        assert summary["roots"][str(path)]["mode"] == "0700"


def test_create_state_roots_is_idempotent(tmp_path: Path, capsys) -> None:
    from liquidity_migration.real_money_arming import main

    roots = tmp_path / "state"
    owner = _owner_env_with_roots(tmp_path, roots)

    assert main(["create-state-roots", "--owner-env", str(owner), "--execute"]) == 0
    capsys.readouterr()
    assert main(["create-state-roots", "--owner-env", str(owner), "--execute"]) == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["created"] == []
    assert len(summary["already_present"]) == 3


def test_create_state_roots_refuses_a_relative_root(tmp_path: Path, capsys) -> None:
    from liquidity_migration.real_money_arming import main

    owner = _owner_env_with_roots(
        tmp_path, tmp_path / "state", ACCOUNT_INTENT_INBOX_ROOT="var/inbox-mainnet"
    )

    assert main(["create-state-roots", "--owner-env", str(owner), "--execute"]) == 2
    assert "is not an absolute path" in capsys.readouterr().err
    assert not (tmp_path / "state").exists()


def test_create_state_roots_refuses_a_root_that_is_a_file(tmp_path: Path, capsys) -> None:
    """Never unlink: the owner is told, and the file stays exactly as it is."""

    from liquidity_migration.real_money_arming import main

    roots = tmp_path / "state"
    roots.mkdir()
    (roots / "inbox").write_text("not a directory", encoding="utf-8")
    owner = _owner_env_with_roots(tmp_path, roots)

    assert main(["create-state-roots", "--owner-env", str(owner), "--execute"]) == 2
    assert "is not a directory" in capsys.readouterr().err
    assert (roots / "inbox").read_text(encoding="utf-8") == "not a directory"
    assert not (roots / "account").exists()


def test_create_state_roots_refuses_a_root_under_the_demo_root(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    """A mainnet journal inside the demo tree would mix two realms' state."""

    from liquidity_migration import real_money_arming
    from liquidity_migration.real_money_arming import main

    demo_root = tmp_path / "demo"
    demo_env = _env_file(
        tmp_path,
        "account-execution.env",
        f"ACCOUNT_EXECUTION_ROOT={demo_root}\n"
        f"ACCOUNT_INTENT_INBOX_ROOT={tmp_path / 'demo-inbox'}\n"
        f"ACCOUNT_CAPTURE_ROOT={tmp_path / 'demo-capture'}\n",
    )
    monkeypatch.setattr(real_money_arming, "DEMO_OWNER_ENV", demo_env)

    owner = _owner_env_with_roots(
        tmp_path, tmp_path / "state", ACCOUNT_EXECUTION_ROOT=str(demo_root / "account-mainnet")
    )
    assert main(["create-state-roots", "--owner-env", str(owner), "--execute"]) == 2
    err = capsys.readouterr().err
    assert "is at or inside" in err and "account-execution.env" in err
    assert not demo_root.exists()
    assert not (tmp_path / "state").exists()

    # An absent demo env file is not an error: a workstation dry run still works.
    monkeypatch.setattr(real_money_arming, "DEMO_OWNER_ENV", tmp_path / "absent.env")
    assert main(["create-state-roots", "--owner-env", str(owner), "--execute"]) == 0


def test_create_state_roots_refuses_a_root_under_the_paper_root(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    """Paper roots live in their own env file; checking only the demo one misses them."""

    from liquidity_migration import real_money_arming
    from liquidity_migration.real_money_arming import main

    paper_root = tmp_path / "paper"
    paper_env = _env_file(
        tmp_path,
        "account-paper-execution.env",
        f"ACCOUNT_EXECUTION_ROOT={paper_root}\n"
        f"ACCOUNT_PAPER_CAPTURE_ROOT={tmp_path / 'paper-capture'}\n",
    )
    monkeypatch.setattr(real_money_arming, "PAPER_OWNER_ENV", paper_env)

    owner = _owner_env_with_roots(
        tmp_path, tmp_path / "state", ACCOUNT_CAPTURE_ROOT=str(paper_root / "capture-mainnet")
    )
    assert main(["create-state-roots", "--owner-env", str(owner), "--execute"]) == 2
    err = capsys.readouterr().err
    assert "is at or inside" in err and "account-paper-execution.env" in err
    assert not paper_root.exists()
    assert not (tmp_path / "state").exists()


def test_create_state_roots_resolves_before_comparing_realms(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    """A lexical compare misses a symlink or ``..`` that lands in the demo tree."""

    from liquidity_migration import real_money_arming
    from liquidity_migration.real_money_arming import main

    demo_root = tmp_path / "demo"
    demo_root.mkdir()
    demo_env = _env_file(
        tmp_path, "account-execution.env", f"ACCOUNT_EXECUTION_ROOT={demo_root}\n"
    )
    monkeypatch.setattr(real_money_arming, "DEMO_OWNER_ENV", demo_env)

    # Same directory, spelled so a string compare cannot see it.
    sideways = tmp_path / "state" / ".." / "demo" / "account-mainnet"
    owner = _owner_env_with_roots(
        tmp_path, tmp_path / "state", ACCOUNT_EXECUTION_ROOT=str(sideways)
    )
    assert main(["create-state-roots", "--owner-env", str(owner), "--execute"]) == 2
    assert "is at or inside" in capsys.readouterr().err
    assert not (demo_root / "account-mainnet").exists()

    link = tmp_path / "link-to-demo"
    link.symlink_to(demo_root)
    owner = _owner_env_with_roots(
        tmp_path, tmp_path / "state", ACCOUNT_EXECUTION_ROOT=str(link / "account-mainnet")
    )
    assert main(["create-state-roots", "--owner-env", str(owner), "--execute"]) == 2
    assert "is at or inside" in capsys.readouterr().err
    assert not (demo_root / "account-mainnet").exists()


def test_create_state_roots_accepts_a_root_that_is_a_symlinked_directory(
    tmp_path: Path, capsys
) -> None:
    """The preflight passes a symlinked root; refusing it here would deadlock arming."""

    from liquidity_migration.real_money_arming import main, preflight

    volume = tmp_path / "volume"
    volume.mkdir()
    roots = tmp_path / "state"
    roots.mkdir()
    (roots / "account").symlink_to(volume)
    owner = _owner_env_with_roots(tmp_path, roots)

    assert main(["create-state-roots", "--owner-env", str(owner), "--execute"]) == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["already_present"] == [str(roots / "account")]
    assert {Path(path).name for path in summary["created"]} == {"inbox", "capture"}

    credential = _filled_credential_env(tmp_path)
    results = preflight(credential_env=credential, owner_env=owner, check_sleeves=False)
    assert next(row for row in results if row.name == "state roots").ok


def test_create_state_roots_refuses_a_broken_symlinked_root(tmp_path: Path, capsys) -> None:
    """mkdir would raise FileExistsError on it; say what is actually wrong."""

    from liquidity_migration.real_money_arming import main

    roots = tmp_path / "state"
    roots.mkdir()
    (roots / "inbox").symlink_to(tmp_path / "nowhere")
    owner = _owner_env_with_roots(tmp_path, roots)

    assert main(["create-state-roots", "--owner-env", str(owner), "--execute"]) == 2
    assert "symlink to nothing" in capsys.readouterr().err
    assert not (roots / "account").exists()


def test_create_state_roots_refuses_an_installed_but_unreadable_realm_env(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    """Silently skipping the realm check is how a mainnet root lands in the demo tree."""

    from liquidity_migration import real_money_arming
    from liquidity_migration.real_money_arming import main

    demo_env = _env_file(tmp_path, "account-execution.env", "ACCOUNT_EXECUTION_ROOT=/demo\n")
    demo_env.chmod(0o000)
    monkeypatch.setattr(real_money_arming, "DEMO_OWNER_ENV", demo_env)
    if os.access(demo_env, os.R_OK):  # running as root: the mode does not bite
        pytest.skip("cannot make a file unreadable as this user")

    owner = _owner_env_with_roots(tmp_path, tmp_path / "state")
    assert main(["create-state-roots", "--owner-env", str(owner), "--execute"]) == 2
    assert "unreadable" in capsys.readouterr().err
    assert not (tmp_path / "state").exists()


def test_create_state_roots_reports_an_unreadable_owner_env(tmp_path: Path, capsys) -> None:
    from liquidity_migration.real_money_arming import main

    assert main(["create-state-roots", "--owner-env", str(tmp_path / "absent.env")]) == 2
    assert "does not exist" in capsys.readouterr().err
