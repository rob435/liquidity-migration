"""The paper fleet executes the demo fleet's decisions, not its own."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from liquidity_migration.account.account_route import ensure_account_route
from liquidity_migration.account.account_service import (
    AccountIntentInbox,
    AccountTargetRequest,
    RequestedIntent,
)
from liquidity_migration.runtime.paper_target_mirror import (
    PaperTargetMirror,
    PaperTargetMirrorError,
    mirror_request,
    mirrored_request_id,
)
from liquidity_migration.runtime.paper_target_mirror_runner import (
    main as mirror_runner_main,
    resolve_owner_uid,
)
from liquidity_migration.account.strategy_event_clock import StrategyEvent
from liquidity_migration.account.strategy_runtime import SleeveTargetIntent
from liquidity_migration.strategy.strategy_target_replay import (
    CAPTURE_KIND,
    CAPTURE_SCHEMA_VERSION,
    CapturedTargetRequest,
    TargetSchedulingCaptureEvent,
    _capture_hash,
    _CAPTURE_GENESIS_HASH,
    _decision_keys_from_requests,
)
from liquidity_migration.core.deterministic_serialization import canonical_json

TS = 1_785_400_000_000_000_000


def _demo_route(tmp_path: Path):
    return ensure_account_route(
        account_id="bybit-demo-unified",
        environment="demo",
        account_root=tmp_path / "demo-acct",
        inbox_root=tmp_path / "demo-inbox",
    )


def _paper_route(tmp_path: Path):
    return ensure_account_route(
        account_id="bybit-paper-unified",
        environment="paper",
        account_root=tmp_path / "paper-acct",
        inbox_root=tmp_path / "paper-inbox",
    )


def _request(route, *, symbol="LAUSDT", notional=25_000.0, suffix="0") -> AccountTargetRequest:
    stage = "exit" if notional == 0.0 else "entry"
    intent = SleeveTargetIntent(
        decision_key=f"carry-target/carry_hold_v3/{suffix}/{stage}/carry_hold/{symbol}",
        target_key=f"carry/carry_hold_v3/carry_hold/{symbol}",
        strategy_id="carry_hold_v3",
        component_id="carry_hold",
        symbol=symbol,
        signed_notional_usdt=notional,
        leverage=2.0,
        reason="carry entry: settled print < -10bp, filters pass",
        metadata={
            "raw_target_notional_usdt": notional,
            "target_weight": 0.1,
            "source": "carry_target_adapter",
        },
    )
    return AccountTargetRequest(
        request_id=f"target-{'a' * 40}{suffix}",
        batch_id=f"carry/carry-target-carry_hold_v3-{suffix}/{stage}/{TS}/0000/abc",
        created_ts_ns=TS,
        route_id=route.route_id,
        account_id=route.account_id,
        environment=route.environment,
        intents=(RequestedIntent(adapter_kind="carry", intent=intent),),
    )


def _tape(path: Path, requests, *, sleeve="carry") -> None:
    """Write a valid hash-chained capture tape containing ``requests``."""

    chain = _CAPTURE_GENESIS_HASH
    lines = []
    for index, request in enumerate(requests):
        stage = "exit" if all(i.intent.signed_notional_usdt == 0.0 for i in request.intents) else "entry"
        captured = CapturedTargetRequest(
            publication_order=0,
            stage=stage,
            request=request,
            request_hash=request.content_hash(),
            arrival_sequence=index + 1,
            durable_queue_state="pending",
            durable_filename=__import__("hashlib")
            .sha256(request.request_id.encode("utf-8"))
            .hexdigest()
            + ".json",
        )
        event = TargetSchedulingCaptureEvent(
            source_event=StrategyEvent(
                source=f"{sleeve}:demo",
                kind="timer",
                event_ts_ns=TS + index,
                ingest_ts_ns=TS + index,
                source_sequence=index + 1,
                payload={
                    "execution_environment": "demo",
                    "strategy_profile": "CarryHoldV3",
                },
            ),
            source_environment="demo",
            sleeve=sleeve,
            strategy_profile="CarryHoldV3",
            requests=(captured,),
            decision_keys=_decision_keys_from_requests((captured,)),
        )
        capture_hash = _capture_hash(chain, event)
        lines.append(
            canonical_json(
                {
                    "schema_version": CAPTURE_SCHEMA_VERSION,
                    "kind": CAPTURE_KIND,
                    "prior_capture_hash": chain,
                    "capture_hash": capture_hash,
                    "capture_event": event.to_dict(),
                }
            )
            + b"\n"
        )
        chain = capture_hash
    path.write_bytes(b"".join(lines))


def _mirror(tmp_path: Path, *, sleeves=("carry",)) -> PaperTargetMirror:
    route = _paper_route(tmp_path)
    return PaperTargetMirror(
        tape_path=tmp_path / "strategy-targets.jsonl",
        route=route,
        inbox=AccountIntentInbox(route),
        sleeves=sleeves,
        cursor_path=tmp_path / "cursor.json",
    )


def _pending(tmp_path: Path) -> list[dict]:
    return [
        json.loads(path.read_bytes())
        for path in sorted((tmp_path / "paper-inbox" / "pending").glob("*.json"))
    ]


class TestRebinding:
    def test_the_request_is_rebound_to_the_paper_route(self, tmp_path: Path) -> None:
        demo, paper = _demo_route(tmp_path), _paper_route(tmp_path)
        mirrored = mirror_request(_request(demo), route=paper, scale=1.0)
        assert mirrored.environment == "paper"
        assert mirrored.account_id == "bybit-paper-unified"
        assert mirrored.route_id == paper.route_id

    def test_identity_that_makes_the_two_fleets_comparable_is_preserved(
        self, tmp_path: Path
    ) -> None:
        demo, paper = _demo_route(tmp_path), _paper_route(tmp_path)
        source = _request(demo)
        mirrored = mirror_request(source, route=paper, scale=1.0)
        assert mirrored.batch_id == source.batch_id
        assert mirrored.created_ts_ns == source.created_ts_ns
        assert mirrored.intents[0].intent.target_key == source.intents[0].intent.target_key
        assert mirrored.intents[0].intent.decision_key == source.intents[0].intent.decision_key
        assert mirrored.intents[0].intent.component_id == source.intents[0].intent.component_id

    def test_every_mirrored_intent_declares_its_provenance(self, tmp_path: Path) -> None:
        demo, paper = _demo_route(tmp_path), _paper_route(tmp_path)
        source = _request(demo)
        metadata = mirror_request(source, route=paper, scale=1.0).intents[0].intent.metadata
        assert metadata["mirror_source_request_id"] == source.request_id
        assert metadata["mirror_source_environment"] == "demo"
        assert metadata["mirror_scale"] == 1.0

    def test_the_paper_request_id_is_derived_and_stable(self, tmp_path: Path) -> None:
        demo, paper = _demo_route(tmp_path), _paper_route(tmp_path)
        source = _request(demo)
        first = mirror_request(source, route=paper, scale=1.0)
        second = mirror_request(source, route=paper, scale=2.0)
        assert first.request_id == mirrored_request_id(source.request_id)
        assert first.request_id != source.request_id
        # Scale must not fork the identity, or a restart would double-publish.
        assert first.request_id == second.request_id


class TestScaling:
    def test_verbatim_is_the_default_and_changes_nothing(self, tmp_path: Path) -> None:
        demo, paper = _demo_route(tmp_path), _paper_route(tmp_path)
        intent = mirror_request(_request(demo), route=paper, scale=1.0).intents[0].intent
        assert intent.signed_notional_usdt == 25_000.0
        assert intent.metadata["raw_target_notional_usdt"] == 25_000.0

    def test_scaling_moves_the_notional_and_its_metadata_together(self, tmp_path: Path) -> None:
        demo, paper = _demo_route(tmp_path), _paper_route(tmp_path)
        intent = mirror_request(_request(demo), route=paper, scale=0.5).intents[0].intent
        assert intent.signed_notional_usdt == 12_500.0
        assert intent.metadata["raw_target_notional_usdt"] == 12_500.0
        # Scale-invariant fields must not move.
        assert intent.metadata["target_weight"] == 0.1
        assert intent.leverage == 2.0

    def test_a_flat_target_stays_exactly_flat_under_any_scale(self, tmp_path: Path) -> None:
        demo, paper = _demo_route(tmp_path), _paper_route(tmp_path)
        source = _request(demo, notional=0.0)
        intent = mirror_request(source, route=paper, scale=0.37).intents[0].intent
        assert intent.signed_notional_usdt == 0.0

    def test_a_non_positive_scale_is_refused(self, tmp_path: Path) -> None:
        demo, paper = _demo_route(tmp_path), _paper_route(tmp_path)
        with pytest.raises(PaperTargetMirrorError):
            mirror_request(_request(demo), route=paper, scale=0.0)


class TestPolling:
    def test_published_demo_targets_land_in_the_paper_inbox(self, tmp_path: Path) -> None:
        demo = _demo_route(tmp_path)
        mirror = _mirror(tmp_path)
        _tape(tmp_path / "strategy-targets.jsonl", [_request(demo, suffix="1")])
        report = mirror.poll()
        assert report.requests_mirrored == 1
        queued = _pending(tmp_path)
        assert len(queued) == 1
        assert queued[0]["environment"] == "paper"

    def test_a_second_poll_over_unchanged_bytes_publishes_nothing(self, tmp_path: Path) -> None:
        demo = _demo_route(tmp_path)
        mirror = _mirror(tmp_path)
        _tape(tmp_path / "strategy-targets.jsonl", [_request(demo, suffix="1")])
        mirror.poll()
        assert mirror.poll().requests_mirrored == 0
        assert len(_pending(tmp_path)) == 1

    def test_a_restart_resumes_from_the_cursor_instead_of_replaying(self, tmp_path: Path) -> None:
        demo = _demo_route(tmp_path)
        mirror = _mirror(tmp_path)
        _tape(tmp_path / "strategy-targets.jsonl", [_request(demo, suffix="1")])
        mirror.poll()
        # A brand-new mirror object, as after a service restart.
        assert _mirror(tmp_path).poll().requests_mirrored == 0
        assert len(_pending(tmp_path)) == 1

    def test_only_appended_bytes_are_reverified_after_the_cursor(self, tmp_path: Path) -> None:
        demo = _demo_route(tmp_path)
        tape = tmp_path / "strategy-targets.jsonl"
        mirror = _mirror(tmp_path)
        _tape(tape, [_request(demo, suffix="1")])
        mirror.poll()
        _tape(tape, [_request(demo, suffix="1"), _request(demo, symbol="ESPUSDT", suffix="2")])
        report = mirror.poll()
        assert report.capture_events_read == 1, "the whole tape was re-read"
        assert report.requests_mirrored == 1
        assert len(_pending(tmp_path)) == 2

    def test_a_first_run_adopts_the_tape_head_instead_of_replaying_history(
        self, tmp_path: Path
    ) -> None:
        """A mirror that has never run has no claim on earlier decisions.

        Starting at offset zero would republish the leader's whole history onto
        a live paper book, which is the replay ``poll`` already refuses when a
        tape shrinks.
        """

        demo = _demo_route(tmp_path)
        tape = tmp_path / "strategy-targets.jsonl"
        _tape(tape, [_request(demo, suffix="1"), _request(demo, symbol="ESPUSDT", suffix="2")])

        mirror = _mirror(tmp_path)
        assert mirror.poll().requests_mirrored == 0
        assert _pending(tmp_path) == []

        # Only what the leader publishes after the adoption is mirrored.
        _tape(
            tape,
            [
                _request(demo, suffix="1"),
                _request(demo, symbol="ESPUSDT", suffix="2"),
                _request(demo, symbol="TLMUSDT", suffix="3"),
            ],
        )
        assert mirror.poll().requests_mirrored == 1
        assert len(_pending(tmp_path)) == 1

    def test_the_adopted_head_is_durable_across_a_restart(self, tmp_path: Path) -> None:
        """Adoption is persisted, so a restart cannot skip the gap it leaves."""

        demo = _demo_route(tmp_path)
        tape = tmp_path / "strategy-targets.jsonl"
        _tape(tape, [_request(demo, suffix="1")])
        _mirror(tmp_path)  # adopts and persists, without polling

        _tape(tape, [_request(demo, suffix="1"), _request(demo, symbol="ESPUSDT", suffix="2")])
        # A restart must resume from the adopted offset, not re-adopt the newer
        # head, or the second request would be silently skipped.
        assert _mirror(tmp_path).poll().requests_mirrored == 1

    def test_other_sleeves_are_left_alone(self, tmp_path: Path) -> None:
        demo = _demo_route(tmp_path)
        _tape(tmp_path / "strategy-targets.jsonl", [_request(demo, suffix="1")], sleeve="long")
        assert _mirror(tmp_path, sleeves=("carry",)).poll().requests_mirrored == 0
        assert _pending(tmp_path) == []

    def test_a_missing_tape_is_reported_unhealthy_rather_than_crashing(self, tmp_path: Path) -> None:
        report = _mirror(tmp_path).poll()
        assert not report.healthy
        assert "missing" in report.detail

    def test_a_truncated_tape_refuses_rather_than_replaying_history(self, tmp_path: Path) -> None:
        demo = _demo_route(tmp_path)
        tape = tmp_path / "strategy-targets.jsonl"
        _tape(tape, [_request(demo, suffix="1"), _request(demo, symbol="ESPUSDT", suffix="2")])
        mirror = _mirror(tmp_path)
        mirror.poll()
        _tape(tape, [_request(demo, suffix="1")])
        with pytest.raises(PaperTargetMirrorError, match="shrank"):
            mirror.poll()

    def test_a_tampered_appended_line_breaks_the_chain(self, tmp_path: Path) -> None:
        demo = _demo_route(tmp_path)
        tape = tmp_path / "strategy-targets.jsonl"
        _tape(tape, [_request(demo, suffix="1")])
        mirror = _mirror(tmp_path)
        mirror.poll()
        with tape.open("ab") as handle:
            handle.write(b'{"schema_version":1,"kind":"account_target_scheduling_capture",'
                         b'"prior_capture_hash":"deadbeef","capture_hash":"deadbeef",'
                         b'"capture_event":{}}\n')
        with pytest.raises(ValueError):
            mirror.poll()

    def test_a_corrupt_cursor_refuses_to_start(self, tmp_path: Path) -> None:
        (tmp_path / "cursor.json").write_text("{not json")
        with pytest.raises(PaperTargetMirrorError):
            _mirror(tmp_path)


class TestPrivilegedOwnerBinding:
    """The mirror is not an owner: it binds to the owner's uid, never creates.

    Running privileged is deliberate -- the demo capture tape is 0600 root:root.
    That means the paper route manifests belong to the paper owner rather than
    to this process, which is exactly the read ``require_account_route``
    documents for a privileged observer.
    """

    def test_a_named_owner_resolves_to_that_uid(self) -> None:
        import getpass
        import os

        assert resolve_owner_uid(None) is None
        assert resolve_owner_uid(getpass.getuser()) == os.geteuid()

    def test_an_unknown_owner_fails_rather_than_falling_back(self) -> None:
        """Falling back to None would silently mean "whoever is running" --
        root, for this unit."""

        with pytest.raises(RuntimeError, match="does not exist"):
            resolve_owner_uid("liquidity-migration-no-such-user")
        with pytest.raises(RuntimeError, match="cannot be blank"):
            resolve_owner_uid("   ")

    def test_the_inbox_accepts_the_owner_uid_it_is_given(self, tmp_path: Path) -> None:
        import os

        route = _paper_route(tmp_path)
        inbox = AccountIntentInbox(route, expected_owner_uid=os.geteuid())
        assert inbox.route == route

    def test_the_inbox_refuses_a_manifest_owned_by_someone_else(self, tmp_path: Path) -> None:
        import os

        route = _paper_route(tmp_path)
        with pytest.raises(Exception, match="owner UID"):
            AccountIntentInbox(route, expected_owner_uid=os.geteuid() + 1)

    def test_the_runner_refuses_an_uninitialized_route_instead_of_creating_one(
        self, tmp_path: Path
    ) -> None:
        """An absent manifest means the paper owner has not started. Creating
        one here would bind the route to root and leave the real owner locked
        out of its own account."""

        from liquidity_migration.account.account_route import AccountRouteMissingError

        with pytest.raises(AccountRouteMissingError):
            mirror_runner_main(
                [
                    "--demo-capture-tape", str(tmp_path / "tape.jsonl"),
                    "--demo-account-root", str(tmp_path / "demo"),
                    "--account-root", str(tmp_path / "never-initialized"),
                    "--inbox-root", str(tmp_path / "never-initialized-inbox"),
                    "--cursor-path", str(tmp_path / "cursor.json"),
                ]
            )
