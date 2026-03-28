import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Dict, Optional

from dotenv import load_dotenv  # type: ignore
from eth_account import Account
from web3 import Web3
from web3.exceptions import TransactionNotFound

# load .env if present so script works both as app module and CLI module
base_dir = Path(__file__).resolve().parent
env_path = base_dir / ".env"
if env_path.exists():
    load_dotenv(dotenv_path=env_path)


def _get_env(name: str, default: Optional[str] = None) -> Optional[str]:
    value = os.environ.get(name)
    if value is None or (isinstance(value, str) and not value.strip()):
        return default
    return value.strip().strip('"')


def get_hush_rpc_url() -> str:
    return _get_env("HUSH_RPC_URL", "https://rpc.hushnetworks.in")


def get_hush_chain_id() -> int:
    raw = _get_env("HUSH_CHAIN_ID", "47756852468")
    try:
        return int(raw)
    except Exception:
        return 47756852468


def get_private_key() -> str:
    key = _get_env("HUSH_PRIVATE_KEY")
    if not key:
        raise ValueError("HUSH_PRIVATE_KEY is not configured")

    # Normalize common handwritten formats
    key = key.strip()
    if key.startswith("\"") and key.endswith("\""):
        key = key[1:-1]
    if key.startswith("0x") or key.startswith("0X"):
        key = key[2:]

    key = key.strip()

    if len(key) != 64 or not all(c in "0123456789abcdefABCDEF" for c in key):
        raise ValueError(
            "HUSH_PRIVATE_KEY must be a 64-char hex string without 0x prefix (only [0-9a-fA-F])"
        )

    return key


def get_contract_address() -> Optional[str]:
    addr = _get_env("HUSH_CONTRACT_ADDRESS")
    if addr:
        return Web3.to_checksum_address(addr)
    return None


def get_wallet_address() -> str:
    pk = get_private_key()
    account = Account.from_key(pk)
    return account.address


def _get_web3() -> Web3:
    rpc = get_hush_rpc_url()
    w3 = Web3(Web3.HTTPProvider(rpc))
    if not w3.is_connected():
        raise ConnectionError(f"Cannot connect to Hush RPC at {rpc}")
    return w3


def hush_status() -> Dict[str, Any]:
    status: Dict[str, Any] = {
        "configured": False,
        "connected": False,
        "chain_id": None,
        "wallet_address": None,
        "target_address": None,
        "latest_block": None,
    }
    try:
        status["wallet_address"] = get_wallet_address()
    except Exception as exc:
        status["error"] = f"wallet error: {exc}"
        return status

    try:
        w3 = _get_web3()
        status["connected"] = True
        status["chain_id"] = w3.eth.chain_id
        status["latest_block"] = w3.eth.block_number
        status["configured"] = True
        target = get_contract_address() or status["wallet_address"]
        status["target_address"] = Web3.to_checksum_address(target)
    except Exception as exc:
        status["error"] = f"rpc error: {exc}"
    return status


def build_hush_payload(event_type: str, payload: Dict[str, Any]) -> bytes:
    wrapped = {
        "event": event_type,
        "payload": payload,
        "timestamp": int(time.time()),
    }
    raw = json.dumps(wrapped, separators=(",", ":"), ensure_ascii=False)
    # Truncate if too large for a normal transaction to avoid reverts.
    if len(raw) > 4096:
        raw = raw[:4096]
    return raw.encode("utf-8")


def send_hush_metric(event_type: str, payload: Dict[str, Any]) -> str:
    w3 = _get_web3()
    chain_id = get_hush_chain_id()
    priv = get_private_key()
    account = Account.from_key(priv)
    from_address = account.address
    target_address = get_contract_address() or from_address

    tx = {
        "nonce": w3.eth.get_transaction_count(from_address),
        "to": Web3.to_checksum_address(target_address),
        "value": 0,
        "gas": 250000,
        "gasPrice": w3.eth.gas_price,
        "chainId": chain_id,
        "data": build_hush_payload(event_type, payload),
    }

    signed = account.sign_transaction(tx)
    raw_tx = signed.rawTransaction
    tx_hash = w3.eth.send_raw_transaction(raw_tx)

    # optionally wait for one receipt, but keep fast
    try:
        receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=30)
        if receipt is None:
            raise TransactionNotFound(tx_hash)
    except Exception as exc:
        logging.warning("Hush metric sent but receipt not available immediately: %s", exc)

    return tx_hash.hex()


def safe_send_hush_metric(event_type: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    
    try:
        tx_hash = send_hush_metric(event_type, payload)
        return {"success": True, "tx_hash": tx_hash}
    except Exception as exc:
        logging.exception("Hush chain metric send failed")
        return {"success": False, "error": str(exc)}
