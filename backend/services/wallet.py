import asyncio
import hashlib
import logging
import re
from datetime import datetime, timezone

from backend.utils import utc_isoformat
from decimal import Decimal
from typing import Literal
from uuid import uuid4

from sqlalchemy.ext.asyncio import AsyncSession

from backend.core.exceptions import (
    AddressValidationError,
    DuplicateWalletError,
    ExtendedKeyValidationError,
    TagValidationError,
    WalletLimitReachedError,
    WalletNotFoundError,
)
from backend.models.user import User
from backend.models.wallet import Wallet
from backend.repositories.snapshot import (
    BalanceSnapshotRepository,
    PriceSnapshotRepository,
)
from backend.repositories.wallet import WalletRepository

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Pure-Python Base58Check helpers (private — not exported)
# ---------------------------------------------------------------------------

_BASE58_CHARS = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_BASE58_MAP = {c: i for i, c in enumerate(_BASE58_CHARS)}


def _b58decode(s: str) -> bytes:
    """Decode a Base58 string to bytes."""
    num = 0
    for char in s:
        if char not in _BASE58_MAP:
            raise ValueError(f"Invalid Base58 character: {char!r}")
        num = num * 58 + _BASE58_MAP[char]
    leading_zeros = len(s) - len(s.lstrip("1"))
    byte_len = (num.bit_length() + 7) // 8 if num > 0 else 1
    return b"\x00" * leading_zeros + num.to_bytes(byte_len, "big")


def _b58decode_check(s: str) -> bytes:
    """Decode Base58Check string; raises ValueError if checksum fails.

    Returns the payload (without the 4-byte checksum).
    """
    decoded = _b58decode(s)
    if len(decoded) < 4:
        raise ValueError("Too short for Base58Check")
    payload, checksum = decoded[:-4], decoded[-4:]
    expected = hashlib.sha256(hashlib.sha256(payload).digest()).digest()[:4]
    if checksum != expected:
        raise ValueError("Base58Check checksum mismatch")
    return payload


def _b58encode(data: bytes) -> str:
    """Encode bytes to a Base58 string."""
    num = int.from_bytes(data, "big")
    result: list[str] = []
    while num > 0:
        num, rem = divmod(num, 58)
        result.append(_BASE58_CHARS[rem])
    leading = len(data) - len(data.lstrip(b"\x00"))
    return "1" * leading + "".join(reversed(result))


def _b58encode_check(payload: bytes) -> str:
    """Encode bytes with Base58Check (appends 4-byte checksum)."""
    checksum = hashlib.sha256(hashlib.sha256(payload).digest()).digest()[:4]
    return _b58encode(payload + checksum)


# ---------------------------------------------------------------------------
# Extended key constants
# ---------------------------------------------------------------------------

_XPUB_VERSION_BYTES = bytes.fromhex("0488B21E")

_XPUB_VERSIONS: dict[str, bytes] = {
    "xpub": bytes.fromhex("0488B21E"),
    "ypub": bytes.fromhex("049D7CB2"),
    "zpub": bytes.fromhex("04B24746"),
}

_TESTNET_PREFIXES: frozenset[str] = frozenset({"tpub", "upub", "vpub"})


# ---------------------------------------------------------------------------
# Public API — detection, validation, normalization
# ---------------------------------------------------------------------------


def detect_input_type(
    raw_input: str,
) -> Literal["individual_btc", "hd_wallet", "kas", "unknown"]:
    """Determine the type of address/key input after normalization.

    Uses prefix/length heuristics only — does NOT perform full validation.
    Used by WalletService to route BTC inputs to the correct validator.
    """
    s = raw_input.strip().replace("\n", "").replace(" ", "")

    if s.startswith("kaspa:"):
        return "kas"

    extended_prefixes = ("xpub", "ypub", "zpub", "tpub", "upub", "vpub")
    if any(s.startswith(p) for p in extended_prefixes):
        return "hd_wallet"

    btc_individual_prefixes = ("bc1q", "bc1p", "1", "3")
    if any(s.startswith(p) for p in btc_individual_prefixes):
        return "individual_btc"

    # Length heuristic for unrecognized extended keys (FR-H05)
    if 107 <= len(s) <= 115:
        return "hd_wallet"

    return "unknown"


def validate_extended_public_key(key: str) -> str | None:
    """Validate an extended public key string.

    Returns None if valid; returns an error message string if invalid.
    Input must already be normalized (stripped of whitespace).

    Validation order: testnet check → prefix check → length check →
    Base58Check verification.
    """
    # 1. Testnet check (runs before length check for a better error message)
    if any(key.startswith(p) for p in _TESTNET_PREFIXES):
        return (
            "Testnet keys are not supported. "
            "Please export a mainnet key from your wallet."
        )

    # 2. Unrecognized prefix
    if not any(key.startswith(p) for p in _XPUB_VERSIONS):
        return (
            "Unrecognized key format. "
            "Bitcoin extended public keys start with xpub, ypub, or zpub."
        )

    # 3. Length check (exactly 111 characters per FR-H03)
    if len(key) != 111:
        return f"Invalid extended public key. Expected 111 characters, got {len(key)}."

    # 4. Base58Check integrity
    try:
        _b58decode_check(key)
    except ValueError:
        return (
            "Invalid extended public key: checksum verification failed. "
            "Please re-export the key from your wallet."
        )

    return None  # Valid


def normalize_to_xpub(key: str) -> str:
    """Convert a ypub or zpub key to xpub version bytes for API compatibility.

    xpub keys are returned unchanged.
    Precondition: key has already been validated by validate_extended_public_key().
    """
    prefix = key[:4]
    if prefix == "xpub":
        return key
    payload = _b58decode_check(key)
    # Replace first 4 bytes (version) with xpub version bytes
    xpub_payload = _XPUB_VERSION_BYTES + payload[4:]
    return _b58encode_check(xpub_payload)


def validate_btc_address(address: str) -> str | None:
    """Returns None if valid, error message string if invalid."""
    address = address.strip().replace("\n", "").replace(" ", "")

    # P2PKH (Legacy) — starts with '1'
    if address.startswith("1"):
        if 25 <= len(address) <= 34 and re.fullmatch(
            r"[123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz]+", address
        ):
            return None
        return "Invalid Bitcoin address format."

    # P2SH — starts with '3'
    if address.startswith("3"):
        if 25 <= len(address) <= 34 and re.fullmatch(
            r"[123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz]+", address
        ):
            return None
        return "Invalid Bitcoin address format."

    # Bech32 SegWit v0 — starts with 'bc1q'
    if address.lower().startswith("bc1q"):
        address = address.lower()
        bech32_chars = r"[023456789acdefghjklmnpqrstuvwxyz]"
        if len(address) in (42, 62) and re.fullmatch(f"bc1q{bech32_chars}+", address):
            return None
        return "Invalid Bitcoin address format."

    # Bech32m Taproot — starts with 'bc1p'
    if address.lower().startswith("bc1p"):
        address = address.lower()
        bech32_chars = r"[023456789acdefghjklmnpqrstuvwxyz]"
        if len(address) == 62 and re.fullmatch(f"bc1p{bech32_chars}+", address):
            return None
        return "Invalid Bitcoin address format."

    return "Invalid Bitcoin address format."


def validate_kas_address(address: str) -> str | None:
    """Returns None if valid, error message string if invalid."""
    address = address.strip().replace("\n", "").replace(" ", "")

    if not address.startswith("kaspa:"):
        return "Invalid Kaspa address format. Kaspa addresses start with 'kaspa:'."

    remainder = address[6:]  # after "kaspa:"
    bech32_chars = r"[023456789acdefghjklmnpqrstuvwxyz]"
    if 60 <= len(remainder) <= 63 and re.fullmatch(bech32_chars + "+", remainder):
        return None

    return "Invalid Kaspa address format. Kaspa addresses start with 'kaspa:'."


class WalletService:
    MAX_WALLETS = 50

    def __init__(
        self,
        db: AsyncSession,
        user: User,
        refresh_service=None,
        history_service=None,
        ws_manager=None,
    ) -> None:
        self.db = db
        self.user = user
        self.refresh_service = refresh_service
        self.history_service = history_service
        self.ws_manager = ws_manager
        self.wallet_repo = WalletRepository(db)
        self.balance_repo = BalanceSnapshotRepository(db)
        self.price_repo = PriceSnapshotRepository(db)
        # Import here to avoid circular imports; ConfigRepository is lightweight
        from backend.repositories.config import ConfigRepository

        self.config_repo = ConfigRepository(db)

    async def list_wallets(self) -> list[dict]:
        from backend.repositories.derived_address import DerivedAddressRepository

        wallets = await self.wallet_repo.list_all(self.user.id)
        derived_repo = DerivedAddressRepository(self.db)
        result = []
        for wallet in wallets:
            balance_snap = await self.balance_repo.get_latest_for_wallet(wallet.id)
            price_snap = await self.price_repo.get_latest(wallet.network)

            balance: str | None = None
            balance_usd: str | None = None
            last_updated: str | None = None

            if balance_snap is not None:
                balance = str(balance_snap.balance)
                last_updated = utc_isoformat(balance_snap.timestamp)
                if price_snap is not None and price_snap.price_usd is not None:
                    balance_usd = str(
                        Decimal(str(balance_snap.balance))
                        * Decimal(str(price_snap.price_usd))
                    )

            history_status = "complete" if balance_snap is not None else "pending"

            # HD wallet fields
            wallet_type = getattr(wallet, "wallet_type", "individual")
            extended_key_type = getattr(wallet, "extended_key_type", None)
            derived_addresses = None
            derived_address_count = None
            derived_address_total = None
            # True while the initial background fetch has not yet completed
            # (no balance snapshot stored yet). history_status is currently a
            # two-state value ("pending"/"complete") — there is no "failed" state
            # stored yet. This means a wallet whose initial fetch permanently
            # failed will also have balance_snap is None and would show
            # hd_loading=True forever. A full fix requires persisting a "failed"
            # status flag (tracked as a future task). For now this is an accepted
            # trade-off: the loading spinner is shown until the next successful
            # refresh writes a snapshot.
            hd_loading = (
                wallet_type == "hd"
                and balance_snap is None
                and history_status != "failed"
            )

            if wallet_type == "hd" and not hd_loading:
                da_rows = await derived_repo.get_by_wallet(wallet.id)
                price_usd = (
                    Decimal(str(price_snap.price_usd))
                    if price_snap is not None and price_snap.price_usd is not None
                    else None
                )
                derived_addresses = [
                    {
                        "address": row.address,
                        "balance_native": row.current_balance_native,
                        "balance_usd": (
                            str(Decimal(row.current_balance_native) * price_usd)
                            if price_usd is not None
                            else None
                        ),
                    }
                    for row in da_rows
                    if Decimal(row.current_balance_native) > 0
                ]
                derived_address_count = len(da_rows)
                # Total count stored in config (may be >200)
                total_str = await self.config_repo.get(f"hd_address_count:{wallet.id}")
                derived_address_total = (
                    int(total_str) if total_str is not None else derived_address_count
                )

            result.append(
                {
                    "id": wallet.id,
                    "network": wallet.network,
                    "address": wallet.address,
                    "tag": wallet.tag,
                    "wallet_type": wallet_type,
                    "extended_key_type": extended_key_type,
                    "balance": balance,
                    "balance_usd": balance_usd,
                    "created_at": utc_isoformat(wallet.created_at),
                    "last_updated": last_updated,
                    "warning": None,
                    "history_status": history_status,
                    "derived_addresses": derived_addresses,
                    "derived_address_count": derived_address_count,
                    "derived_address_total": derived_address_total,
                    "hd_loading": hd_loading,
                }
            )
        return result

    async def add_wallet(self, network: str, address: str, tag: str | None) -> Wallet:
        # 1. Check wallet limit
        count = await self.wallet_repo.count_by_user(self.user.id)
        if count >= self.MAX_WALLETS:
            raise WalletLimitReachedError(
                "Wallet limit reached (50). Remove a wallet to add a new one."
            )

        # 2. Normalize address
        address = address.strip().replace("\n", "").replace(" ", "")

        # 3. Route by network and input type
        if network == "BTC":
            input_type = detect_input_type(address)
            if input_type == "hd_wallet":
                return await self._add_hd_wallet(address, tag)
            # individual_btc or unknown — fall through to existing individual path
            error = validate_btc_address(address)
        elif network == "KAS":
            error = validate_kas_address(address)
        else:
            raise ValueError(f"Unsupported network: {network}")
        if error:
            raise AddressValidationError(error)

        # 4. Check duplicate (BTC case-insensitive, KAS exact)
        normalized = address.lower() if network == "BTC" else address
        exists = await self.wallet_repo.exists_by_address(
            self.user.id, network, normalized
        )
        if exists:
            raise DuplicateWalletError("This wallet address is already being tracked.")

        # 5. Handle tag
        if not tag or not tag.strip():
            tag = await self._generate_default_tag(network)
        else:
            tag = tag.strip()
            if len(tag) > 50:
                raise TagValidationError("Tag must be 50 characters or fewer.")
            if await self.wallet_repo.tag_exists(self.user.id, tag):
                raise TagValidationError("A wallet with this tag already exists.")

        # 6. Persist wallet
        wallet = Wallet(
            id=str(uuid4()),
            user_id=self.user.id,
            network=network,
            address=address,
            tag=tag,
            created_at=datetime.now(timezone.utc),
        )
        await self.wallet_repo.create(wallet)

        # 7. Spawn background tasks (non-blocking)
        asyncio.create_task(self._fetch_initial_data(wallet))

        return wallet

    async def update_tag(self, wallet_id: str, new_tag: str) -> Wallet:
        wallet = await self.wallet_repo.get_by_id(wallet_id, self.user.id)
        if wallet is None:
            raise WalletNotFoundError(f"Wallet {wallet_id} not found.")

        new_tag = new_tag.strip()
        if len(new_tag) > 50:
            raise TagValidationError("Tag must be 50 characters or fewer.")
        if await self.wallet_repo.tag_exists(
            self.user.id, new_tag, exclude_wallet_id=wallet_id
        ):
            raise TagValidationError("A wallet with this tag already exists.")

        await self.wallet_repo.update_tag(wallet_id, new_tag)
        wallet.tag = new_tag
        return wallet

    async def remove_wallet(self, wallet_id: str) -> None:
        wallet = await self.wallet_repo.get_by_id(wallet_id, self.user.id)
        if wallet is None:
            raise WalletNotFoundError(f"Wallet {wallet_id} not found.")
        await self.wallet_repo.delete(wallet_id)
        # Clean up HD-wallet config keys (no-op for individual wallets)
        for prefix in (
            f"hd_address_count:{wallet_id}",
            f"hd_bal_tip:{wallet_id}",
            f"hd_sync_tip:{wallet_id}",
        ):
            await self.config_repo.delete_by_prefix(prefix)

    async def retry_history_import(self, wallet_id: str) -> None:
        wallet = await self.wallet_repo.get_by_id(wallet_id, self.user.id)
        if wallet is None:
            raise WalletNotFoundError(f"Wallet {wallet_id} not found.")
        if self.history_service is not None:
            asyncio.create_task(self._run_history_import(wallet))

    async def _add_hd_wallet(self, key: str, tag: str | None) -> Wallet:
        """Validate, persist, and trigger background fetch for an HD wallet."""
        # 1. Validate extended public key
        error = validate_extended_public_key(key)
        if error:
            # Raise the specific subclass so callers can distinguish HD key errors
            raise ExtendedKeyValidationError(error)

        # 2. Check for duplicate — exact-match, case-sensitive per FR-H06
        exists = await self.wallet_repo.exists_by_address_exact(
            self.user.id, "BTC", key
        )
        if exists:
            raise DuplicateWalletError("This HD wallet key is already being tracked.")

        # 3. Determine key type from prefix
        key_type = key[:4]  # "xpub", "ypub", or "zpub"

        # 4. Resolve tag
        if not tag or not tag.strip():
            tag = await self._generate_hd_default_tag()
        else:
            tag = tag.strip()
            if len(tag) > 50:
                raise TagValidationError("Tag must be 50 characters or fewer.")
            if await self.wallet_repo.tag_exists(self.user.id, tag):
                raise TagValidationError("A wallet with this tag already exists.")

        # 5. Persist and commit before spawning background task.
        # The commit must happen here so that _fetch_initial_hd_data can query
        # the wallet row from the DB without hitting an uncommitted write.
        # (TECH_SPEC_HD_WALLETS.md §4.7.b — commit-before-create_task ordering)
        wallet = Wallet(
            id=str(uuid4()),
            user_id=self.user.id,
            network="BTC",
            address=key,  # extended_public_key stored in address column (per spec §6.1)
            tag=tag,
            wallet_type="hd",
            extended_key_type=key_type,
            created_at=datetime.now(timezone.utc),
        )
        await self.wallet_repo.create(wallet)
        await self.db.commit()

        # 6. Trigger background tasks (non-blocking)
        asyncio.create_task(self._fetch_initial_hd_data(wallet))

        return wallet

    async def _generate_hd_default_tag(self) -> str:
        """Return the next available "BTC HD Wallet #n" tag."""
        n = 1
        while True:
            candidate = f"BTC HD Wallet #{n}"
            if not await self.wallet_repo.tag_exists(self.user.id, candidate):
                return candidate
            n += 1

    async def _fetch_initial_hd_data(self, wallet: Wallet) -> None:
        """Background task: initial balance + history import for HD wallet."""
        if self.refresh_service is not None:
            try:
                await self.refresh_service.refresh_single_hd_wallet(wallet)
            except Exception:
                logger.warning(
                    "Could not fetch initial balance for HD wallet %s", wallet.tag
                )
        if self.history_service is not None:
            try:
                await self.history_service.full_import_hd(wallet)
            except Exception:
                logger.warning("HD wallet history import failed for %s", wallet.tag)
        if self.ws_manager is not None:
            try:
                await self.ws_manager.broadcast(
                    "wallet:added", {"wallet_id": wallet.id}
                )
            except Exception:
                logger.warning(
                    "WebSocket broadcast failed for HD wallet %s", wallet.tag
                )

    async def _generate_default_tag(self, network: str) -> str:
        prefix = "BTC" if network == "BTC" else "KAS"
        n = 1
        while True:
            candidate = f"{prefix} Wallet #{n}"
            exists = await self.wallet_repo.tag_exists(self.user.id, candidate)
            if not exists:
                return candidate
            n += 1

    async def _fetch_initial_data(self, wallet: Wallet) -> None:
        """Fetch current balance + start history import. Runs as background task."""
        if self.refresh_service is not None:
            try:
                await self.refresh_service.refresh_single_wallet(wallet)
            except Exception:
                logger.warning("Could not fetch initial balance for %s", wallet.tag)
        if self.history_service is not None:
            try:
                await self.history_service.full_import(wallet)
            except Exception:
                logger.warning("History import failed for %s", wallet.tag)
        if self.ws_manager is not None:
            try:
                await self.ws_manager.broadcast(
                    "wallet:added", {"wallet_id": wallet.id}
                )
            except Exception:
                logger.warning("WebSocket broadcast failed for %s", wallet.tag)

    async def _run_history_import(self, wallet: Wallet) -> None:
        try:
            await self.history_service.full_import(wallet)
        except Exception:
            logger.warning("History import retry failed for %s", wallet.tag)
