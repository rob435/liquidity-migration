"""Process leases for account owners and Bybit demo mutation authority."""

from __future__ import annotations

import fcntl
import hashlib
import hmac
import json
import os
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping


def _credential_fingerprint(api_key: str) -> str:
    normalized = str(api_key).strip()
    if not normalized:
        raise ValueError("demo API key is required for account identity")
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


_AUTHENTICATED_IDENTITY_TOKEN = object()


@dataclass(frozen=True, slots=True, init=False)
class DemoAccountIdentity:
    """Credential-bound identity of one Bybit demo account."""

    user_id: str
    api_key_sha256: str = field(repr=False)
    environment: str = "demo"

    def __init__(
        self,
        *,
        user_id: str,
        api_key_sha256: str,
        environment: str,
        _authenticated_identity_token: object | None = None,
    ) -> None:
        if _authenticated_identity_token is not _AUTHENTICATED_IDENTITY_TOKEN:
            raise TypeError(
                "DemoAccountIdentity cannot be constructed directly; use "
                "DemoAccountIdentity.from_api_key_info()"
            )
        object.__setattr__(self, "user_id", user_id)
        object.__setattr__(self, "api_key_sha256", api_key_sha256)
        object.__setattr__(self, "environment", environment)

    @classmethod
    def from_api_key_info(
        cls,
        *,
        api_key: str,
        api_key_info: Mapping[str, Any],
        environment: str = "demo",
    ) -> "DemoAccountIdentity":
        """Build identity from the authenticated ``query-api`` response.

        ``userID`` is the venue account identity.  The key fingerprint binds a
        held lease capability to the credential that produced that response
        without writing the credential itself to disk.
        """

        normalized_environment = str(environment).strip().lower()
        if normalized_environment != "demo":
            raise RuntimeError(
                "Bybit account mutation identity requires environment='demo'"
            )
        normalized_api_key = str(api_key).strip()
        reported_api_key = str(api_key_info.get("apiKey") or "").strip()
        if not reported_api_key or not hmac.compare_digest(
            normalized_api_key,
            reported_api_key,
        ):
            raise RuntimeError(
                "Bybit API key metadata does not match the configured demo API key"
            )
        raw_user_id = api_key_info.get("userID")
        if isinstance(raw_user_id, bool):
            raw_user_id = None
        try:
            numeric_user_id = int(str(raw_user_id).strip())
        except (TypeError, ValueError) as exc:
            raise RuntimeError(
                "Bybit demo API key metadata is missing a valid userID; "
                "cannot derive the canonical account-owner lease"
            ) from exc
        if numeric_user_id <= 0:
            raise RuntimeError(
                "Bybit demo API key metadata has a non-positive userID; "
                "cannot derive the canonical account-owner lease"
            )
        return cls(
            user_id=str(numeric_user_id),
            api_key_sha256=_credential_fingerprint(normalized_api_key),
            environment=normalized_environment,
            _authenticated_identity_token=_AUTHENTICATED_IDENTITY_TOKEN,
        )


def _canonical_demo_lease_directory() -> Path:
    """Return the fixed host-global namespace; there is no environment override."""

    return Path("/run/lock/liquidity-migration")


def canonical_demo_account_lease_path(identity: DemoAccountIdentity) -> Path:
    """Canonical lock path shared by all credentials for one demo account."""

    return _canonical_demo_lease_directory() / (
        f"bybit-{identity.environment}-user-{identity.user_id}.lock"
    )


class AccountOwnerLease:
    """Kernel-enforced advisory lock held for a local owner's entire lifetime.

    This path-based lease remains appropriate for the paper account, whose
    identity is its canonical local route.  It is deliberately *not* accepted
    as Bybit mutation authority; venue mutation requires the subclass below.
    """

    def __init__(self, path: str | Path) -> None:
        # Keep the lexical path rather than resolving it: a symlink must be
        # refused, not normalized into an apparently canonical lease target.
        self.path = Path(os.path.abspath(Path(path).expanduser()))
        self._file: Any | None = None
        self._holder_pid: int | None = None
        self._lease_identity: tuple[int, int] | None = None

    @property
    def held(self) -> bool:
        handle = self._file
        identity = self._lease_identity
        if (
            handle is None
            or handle.closed
            or self._holder_pid != os.getpid()
            or identity is None
        ):
            return False
        try:
            descriptor_metadata = os.fstat(handle.fileno())
            path_metadata = self.path.lstat()
        except OSError:
            return False
        return (
            stat.S_ISREG(descriptor_metadata.st_mode)
            and stat.S_ISREG(path_metadata.st_mode)
            and descriptor_metadata.st_nlink == 1
            and path_metadata.st_nlink == 1
            and (descriptor_metadata.st_dev, descriptor_metadata.st_ino) == identity
            and (path_metadata.st_dev, path_metadata.st_ino) == identity
        )

    def _lease_metadata(self) -> dict[str, Any]:
        return {"pid": os.getpid()}

    def acquire(self) -> None:
        if self._file is not None:
            if self.held:
                return
            raise RuntimeError("account execution owner lease belongs to another process")
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        flags = (
            os.O_RDWR
            | os.O_CREAT
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        try:
            descriptor = os.open(self.path, flags, 0o600)
        except OSError as exc:
            raise RuntimeError(
                f"account execution owner lease cannot be opened safely: {self.path}"
            ) from exc
        try:
            descriptor_metadata = os.fstat(descriptor)
            path_metadata = self.path.lstat()
            descriptor_identity = (
                descriptor_metadata.st_dev,
                descriptor_metadata.st_ino,
            )
            if (
                not stat.S_ISREG(descriptor_metadata.st_mode)
                or not stat.S_ISREG(path_metadata.st_mode)
                or descriptor_metadata.st_nlink != 1
                or path_metadata.st_nlink != 1
                or (path_metadata.st_dev, path_metadata.st_ino)
                != descriptor_identity
            ):
                raise RuntimeError(
                    "account execution owner lease must be one path-bound, "
                    "single-link regular file"
                )
            os.fchmod(descriptor, 0o600)
            handle = os.fdopen(descriptor, "r+", encoding="utf-8")
        except BaseException:
            os.close(descriptor)
            raise
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            handle.seek(0)
            owner = handle.read().strip()
            handle.close()
            raise RuntimeError(
                "account execution owner lease is already held"
                + (f": {owner}" if owner else "")
            ) from exc
        except BaseException:
            handle.close()
            raise
        handle.seek(0)
        handle.truncate()
        handle.write(json.dumps(self._lease_metadata(), sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
        self._file = handle
        self._holder_pid = os.getpid()
        self._lease_identity = descriptor_identity
        if not self.held:
            self.close()
            raise RuntimeError(
                "account execution owner lease path changed during acquisition"
            )

    def close(self) -> None:
        handle = self._file
        self._file = None
        self._holder_pid = None
        self._lease_identity = None
        if handle is None:
            return
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()

    def __enter__(self) -> "AccountOwnerLease":
        self.acquire()
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()


class DemoAccountMutationLease(AccountOwnerLease):
    """Held canonical lease capability required for every demo mutation."""

    def __init__(self, identity: DemoAccountIdentity) -> None:
        if type(identity) is not DemoAccountIdentity:
            raise TypeError(
                "DemoAccountMutationLease requires an authenticated "
                "DemoAccountIdentity"
            )
        self._identity = identity
        super().__init__(canonical_demo_account_lease_path(identity))

    @property
    def identity(self) -> DemoAccountIdentity:
        return self._identity

    def acquire(self) -> None:
        try:
            super().acquire()
        except OSError as exc:
            raise RuntimeError(
                f"cannot acquire canonical Bybit demo account lease at {self.path}; "
                "fix host lock-directory permissions (no alternate path is allowed)"
            ) from exc

    def _lease_metadata(self) -> dict[str, Any]:
        return {
            "api_key_sha256": self.identity.api_key_sha256,
            "environment": self.identity.environment,
            "pid": os.getpid(),
            "user_id": self.identity.user_id,
            "venue": "bybit",
        }

    def require_held_for(
        self,
        *,
        api_key: str,
        environment: str,
        action: str,
    ) -> None:
        """Prove this process still owns the canonical credential/account lease."""

        expected_path = canonical_demo_account_lease_path(self.identity)
        if self.path != expected_path or not self.held:
            raise RuntimeError(
                f"Refusing to {action}: the canonical Bybit demo account mutation "
                "lease is not currently held by this process"
            )
        observed_fingerprint = _credential_fingerprint(api_key)
        if not hmac.compare_digest(
            self.identity.api_key_sha256,
            observed_fingerprint,
        ):
            raise RuntimeError(
                f"Refusing to {action}: the held Bybit demo account mutation "
                "lease belongs to a different API credential"
            )
        if not hmac.compare_digest(
            self.identity.environment,
            str(environment).strip().lower(),
        ):
            raise RuntimeError(
                f"Refusing to {action}: the held Bybit account mutation lease "
                "belongs to a different environment"
            )
