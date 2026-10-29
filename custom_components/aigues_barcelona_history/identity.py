"""Canonical provider contract identifiers shared by every entry point."""

from collections.abc import Iterable


def canonical_contracts(contracts: Iterable[str]) -> list[str]:
    """Return ordered unique IDs, rejecting malformed provider/config data."""
    if isinstance(contracts, (str, bytes, dict)):
        raise ValueError("Contracts must be a collection of identifiers")
    result = {}
    for contract in contracts:
        if not isinstance(contract, str) or not contract.strip():
            raise ValueError("A contract identifier must be a non-empty string")
        result[contract.strip().upper()] = None
    return list(result)
