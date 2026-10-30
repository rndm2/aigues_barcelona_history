"""Canonical provider identities and contract validation for every entry point."""

from collections.abc import Iterable


def canonical_account(value: str) -> str:
    """Compare configured accounts and JWT identity hints consistently."""
    if not isinstance(value, str):
        raise ValueError("An account identifier must be a string")
    return value.strip().upper()


def token_matches_account(client, account: str) -> bool:
    """Check an identity hint; a server request must still validate the session."""
    claimed = client._return_token_field("name")
    return isinstance(claimed, str) and canonical_account(claimed) == canonical_account(account)


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


def contracts_from_response(rows) -> list[str]:
    """Validate the provider's authenticated contract-list response."""
    if not isinstance(rows, list):
        raise ValueError("Provider contracts must be a list")
    return canonical_contracts(row["contractDetail"]["contractNumber"] for row in rows)


def contracts_include(available, required) -> bool:
    """Require every configured contract before adopting another session."""
    return set(canonical_contracts(required)).issubset(canonical_contracts(available))
