"""Typed authority-v2 errors and the cooperative TTY presence guard."""

from __future__ import annotations

from typing import TextIO


class AuthorityError(RuntimeError):
    """Base class for an authority-v2 refusal."""


class ReceiptNotFound(AuthorityError):
    pass


class ReceiptIntegrityError(AuthorityError):
    pass


class ProposalIntegrityError(AuthorityError):
    pass


class ProposalDecided(AuthorityError):
    pass


class StaleAuthority(AuthorityError):
    pass


class HumanPresenceRequired(AuthorityError):
    pass


class ConfirmationMismatch(AuthorityError):
    pass


def require_confirmation(
    expected: str,
    *,
    stdin: TextIO,
    stdout: TextIO,
) -> None:
    """Require an exact action-bound confirmation on two real TTY streams."""

    if not stdin.isatty() or not stdout.isatty():
        raise HumanPresenceRequired(
            "receipt issuance requires interactive TTY stdin and stdout"
        )
    stdout.write(f"Type {expected} to issue the owner receipt: ")
    stdout.flush()
    actual = stdin.readline()
    if actual.endswith("\n"):
        actual = actual[:-1]
    if actual.endswith("\r"):
        actual = actual[:-1]
    if actual != expected:
        raise ConfirmationMismatch(
            f"confirmation did not exactly match {expected!r}"
        )


def short_id(value: str) -> str:
    return value.split(":", 1)[-1][:12]
