"""Web3 wallet tools with cold/hot mode security model.

SECURITY MODEL:
- Private keys NEVER stored in prompt, memory, or logs
- Keys stored ONLY in the Credentials Vault
- Cold mode (default): only read operations available
- Hot mode: send/swap enabled ONLY after explicit passphrase unlock
- Every transaction logged with full audit trail
- HITL approval required for all on-chain writes
"""

from __future__ import annotations

import logging
from typing import Any

from langchain_core.tools import tool

from olano.tools._credentials import get_credential

logger = logging.getLogger(__name__)

# Hot mode state (per-session, not persisted)
_hot_mode_enabled: dict[str, bool] = {}  # chain → bool
_hot_mode_expiry: dict[str, float] = {}  # chain → unix timestamp


def _is_hot(chain: str = "ethereum") -> bool:
    import time

    if not _hot_mode_enabled.get(chain):
        return False
    if time.time() > _hot_mode_expiry.get(chain, 0):
        _hot_mode_enabled[chain] = False
        return False
    return True


def _get_web3(chain: str = "ethereum") -> tuple[Any | None, str | None]:
    """Return (web3, error_string). One is always None."""
    from olano.tools._install_hint import optional_import

    web3_mod, err = optional_import("web3", purpose="Web3 wallet operations")
    if err or web3_mod is None:
        return None, err
    Web3 = web3_mod.Web3

    rpc_map = {
        "ethereum": get_credential("ETH_RPC_URL", "https://mainnet.infura.io/v3/YOUR_KEY"),
        "bsc": get_credential("BSC_RPC_URL", "https://bsc-dataseed.binance.org"),
        "polygon": get_credential("POLYGON_RPC_URL", "https://polygon-rpc.com"),
    }
    url = rpc_map.get(chain, rpc_map["ethereum"])
    try:
        return Web3(Web3.HTTPProvider(url)), None
    except Exception as exc:
        return None, f"[tool-error] wallet: web3 init failed: {exc}"


def _get_private_key(address: str) -> tuple[str | None, str | None]:
    """Retrieve private key from vault — never from env or prompt.

    Returns (key, error_string). One is always None.
    """
    try:
        from olano.vault.resolver import get_vault

        vault = get_vault()
        if not vault:
            return None, "[missing-credentials] Vault not initialized. Run: olano vault init"
        try:
            return vault.get(f"WALLET_{address.upper()}_PRIVATE_KEY"), None
        except Exception:
            return None, (
                f"[missing-credentials] Private key for {address} not found in vault. "
                f"Store it with: olano vault set WALLET_{address.upper()}_PRIVATE_KEY"
            )
    except Exception as exc:
        return None, f"[tool-error] wallet: vault access failed: {exc}"


# ─────────────────────────────────── Cold mode tools (always available) ──────


@tool
def wallet_get_balance(address: str, chain: str = "ethereum") -> str:
    """Get wallet ETH/native token balance. Always available (cold mode)."""
    w3, err = _get_web3(chain)
    if err:
        return err
    assert w3 is not None
    try:
        checksum_addr = w3.to_checksum_address(address)
        balance_wei = w3.eth.get_balance(checksum_addr)
        balance_eth = w3.from_wei(balance_wei, "ether")
        return f"{address}: {balance_eth:.6f} ETH ({chain})"
    except Exception as exc:
        return f"[tool-error] wallet_get_balance: {exc}"


@tool
def wallet_get_transactions(address: str, chain: str = "ethereum", limit: int = 5) -> str:
    """Get recent transactions for a wallet address (read-only, cold mode)."""
    from olano.tools._install_hint import optional_import

    requests, err = optional_import("requests", purpose="wallet transaction lookup")
    if err:
        return err
    assert requests is not None
    try:
        api_key = get_credential("ETHERSCAN_API_KEY", "")
        url = (
            f"https://api.etherscan.io/api?module=account&action=txlist&address={address}"
            f"&sort=desc&apikey={api_key}&offset={limit}&page=1"
        )
        resp = requests.get(url, timeout=10)
        resp.raise_for_status()
        txns = resp.json().get("result", [])
        if isinstance(txns, str):
            return f"[tool-error] wallet_get_transactions: API error: {txns}"
        txns = txns[:limit]
        lines = []
        for tx in txns:
            val = int(tx.get("value", 0)) / 1e18
            tx_hash = tx.get("hash", "")
            tx_from = tx.get("from", "")
            lines.append(
                f"tx={tx_hash[:10]}... from={tx_from[:8]}... "
                f"value={val:.4f} ETH status={tx.get('txreceipt_status', '?')}"
            )
        return "\n".join(lines) or "No transactions found."
    except Exception as exc:
        return f"[tool-error] wallet_get_transactions: {exc}"


@tool
def wallet_unlock_hot_mode(
    passphrase: str, chain: str = "ethereum", duration_minutes: int = 30
) -> str:
    """
    Unlock hot mode for the specified chain.
    Enables send/swap tools for the given duration.
    REQUIRES: correct vault passphrase.
    """
    import time

    # Verify passphrase by trying to open vault
    try:
        from olano.vault.store import VaultStore

        VaultStore.open(passphrase=passphrase)  # Will raise if wrong passphrase
    except Exception as e:
        return f"Hot mode unlock FAILED: {e}"

    _hot_mode_enabled[chain] = True
    _hot_mode_expiry[chain] = time.time() + duration_minutes * 60
    logger.warning("WALLET: Hot mode enabled for chain=%s duration=%dm", chain, duration_minutes)
    return (
        f"Hot mode enabled for {chain} ({duration_minutes} minutes). "
        "Wallet send/swap tools are now available. USE WITH CAUTION."
    )


@tool
def wallet_lock(chain: str = "ethereum") -> str:
    """Lock hot mode immediately for the specified chain."""
    _hot_mode_enabled[chain] = False
    logger.info("WALLET: Hot mode locked for chain=%s", chain)
    return f"Hot mode locked for {chain}. Send/swap tools disabled."


# ─────────────────────────────────── Hot mode tools ──────────────────────────


@tool
def wallet_send(
    from_address: str,
    to_address: str,
    amount_eth: float,
    chain: str = "ethereum",
) -> str:
    """
    Send ETH from one wallet to another.
    REQUIRES: hot mode enabled AND HITL approval.
    Private key fetched from vault — never from memory.
    """
    if not _is_hot(chain):
        return (
            "[tool-error] wallet_send: Hot mode not enabled. "
            "Use wallet_unlock_hot_mode() first, then confirm with your passphrase."
        )

    try:
        logger.warning(
            "WALLET SEND: from=%s to=%s amount=%.6f ETH chain=%s — REQUIRES APPROVAL",
            from_address,
            to_address,
            amount_eth,
            chain,
        )

        # Human approval is enforced upstream by the unified HITL layer:
        # wallet_send is in the `financial` interrupt category
        # (olano/security/hitl_policy.py), so the runner pauses for approval
        # before this tool body ever runs (dashboard card / Telegram buttons).
        # Hot mode (checked above) is the second gate. The old
        # in-tool `loop.run_until_complete(...)` confirmation was removed: it
        # raised RuntimeError on the already-running server event loop and fell
        # back to a blocking console `input()`, which is broken in any async/
        # server context — and it double-prompted now that HITL gates this tool.
        w3, err = _get_web3(chain)
        if err:
            return err
        assert w3 is not None
        private_key, err = _get_private_key(from_address)
        if err:
            return err

        from_checksum = w3.to_checksum_address(from_address)
        to_checksum = w3.to_checksum_address(to_address)

        nonce = w3.eth.get_transaction_count(from_checksum)
        gas_price = w3.eth.gas_price
        tx = {
            "nonce": nonce,
            "to": to_checksum,
            "value": w3.to_wei(amount_eth, "ether"),
            "gas": 21000,
            "gasPrice": gas_price,
            "chainId": w3.eth.chain_id,
        }
        signed = w3.eth.account.sign_transaction(tx, private_key)
        tx_hash = w3.eth.send_raw_transaction(signed.rawTransaction)
        tx_hex = tx_hash.hex()

        logger.warning(
            "WALLET TX SENT: chain=%s from=%s to=%s amount=%.6f ETH tx=%s",
            chain,
            from_address,
            to_address,
            amount_eth,
            tx_hex,
        )
        return f"Transaction sent: {tx_hex}"
    except Exception as exc:
        return f"[tool-error] wallet_send: {exc}"


WALLET_COLD_TOOLS = [
    wallet_get_balance,
    wallet_get_transactions,
    wallet_unlock_hot_mode,
]

WALLET_HOT_TOOLS = WALLET_COLD_TOOLS + [
    wallet_send,
    wallet_lock,
]


from olano.tools._toolkit import make_toolkit_api

get_tools, get_tool = make_toolkit_api(globals())
