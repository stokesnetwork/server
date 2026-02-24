import asyncio
import logging
import os
from typing import Any

import sqlalchemy
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from constants import ADDRESS_PREFIX, USE_SCRIPT_FOR_ADDRESS
from dbsession import async_session, async_session_blocks
from kaspad.KaspadMultiClient import KaspadMultiClient
from models.Block import Block
from models.BlockParent import BlockParent
from models.BlockTransaction import BlockTransaction
from models.Subnetwork import Subnetwork
from models.Transaction import Transaction, TransactionInput, TransactionOutput
from models.TransactionAcceptance import TransactionAcceptance
from models.TxAddrMapping import TxAddrMapping, TxScriptMapping
from models.Variable import KeyValueModel

_logger = logging.getLogger(__name__)


def _norm_hex(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        value = str(value)
    value = value.strip().lower()
    if not value:
        return None
    if value.startswith("0x"):
        value = value[2:]
    # bytes.fromhex requires an even number of characters.
    if len(value) % 2 == 1:
        value = "0" + value
    # Validate hex.
    for c in value:
        if c not in "0123456789abcdef":
            return None
    return value


def _get_kaspad_hosts() -> list[str]:
    hosts: list[str] = []
    i = 1
    while True:
        v = os.getenv(f"KASPAD_HOST_{i}")
        if not v:
            break
        hosts.append(v)
        i += 1

    if not hosts:
        raise RuntimeError("No KASPAD_HOST_1..N configured")

    return hosts


async def _get_or_create_subnetwork_id(session, subnetwork_id: str) -> int:
    if subnetwork_id is None:
        subnetwork_id = ""

    existing = await session.execute(select(Subnetwork).where(Subnetwork.subnetwork_id == subnetwork_id))
    existing = existing.scalar_one_or_none()
    if existing:
        return int(existing.id)

    max_id = await session.execute(select(Subnetwork.id).order_by(Subnetwork.id.desc()).limit(1))
    max_id = max_id.scalar_one_or_none()
    new_id = int(max_id or 0) + 1

    await session.execute(
        insert(Subnetwork)
        .values([{"id": new_id, "subnetwork_id": subnetwork_id}])
        .on_conflict_do_nothing(index_elements=["id"])
    )
    return new_id


async def _vars_get(session, key: str) -> str | None:
    row = await session.execute(select(KeyValueModel).where(KeyValueModel.key == key))
    row = row.scalar_one_or_none()
    return row.value if row else None


async def _vars_set(session, key: str, value: str) -> None:
    await session.execute(
        insert(KeyValueModel)
        .values([{"key": key, "value": value}])
        .on_conflict_do_update(index_elements=["key"], set_={"value": value})
    )


async def _fetch_prev_outpoint(session, txid: str, index: int) -> tuple[str | None, int | None, str | None]:
    row = await session.execute(
        select(TransactionOutput.script_public_key, TransactionOutput.amount, TransactionOutput._script_public_key_address)
        .where(TransactionOutput.transaction_id == txid)
        .where(TransactionOutput.index == index)
        .limit(1)
    )
    row = row.first()
    if not row:
        return None, None, None

    script_public_key, amount, address = row
    return script_public_key, int(amount) if amount is not None else None, address


def _as_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except Exception:
        return None


def _debug_hex_field(name: str, value: Any) -> None:
    if value is None:
        return
    if not isinstance(value, str):
        value = str(value)
    v = value.strip().lower()
    if v.startswith("0x"):
        v = v[2:]
    if len(v) % 2 == 1:
        v = "0" + v
    try:
        bytes.fromhex(v)
    except Exception:
        _logger.error("Invalid hex for %s: %r", name, value)


def _qualify_address(addr: Any) -> str | None:
    if addr is None:
        return None
    if not isinstance(addr, str):
        addr = str(addr)
    addr = addr.strip()
    if not addr:
        return None

    prefix = f"{ADDRESS_PREFIX}:"
    if addr.startswith(prefix):
        return addr
    # If it's already qualified with another prefix, keep it as-is.
    if ":" in addr:
        return addr
    return prefix + addr


async def index_forever() -> None:
    kaspad = KaspadMultiClient(_get_kaspad_hosts())
    await kaspad.initialize_all()

    poll_seconds = float(os.getenv("INDEXER_POLL_SECONDS", "2"))
    batch_limit = int(os.getenv("INDEXER_BATCH_LIMIT", "0"))
    vars_key = os.getenv("INDEXER_CURSOR_KEY", "indexer_low_hash")
    start_from = os.getenv("INDEXER_START_FROM", "")

    while True:
        async with async_session() as s:
            async with async_session_blocks() as sb:
                try:
                    dag = await kaspad.request("getBlockDagInfoRequest")
                    dag = dag.get("getBlockDagInfoResponse") or {}
                    pruning_point = dag.get("pruningPointHash")
                    virtual_parents = dag.get("virtualParentHashes") or []
                    virtual_parent = virtual_parents[0] if virtual_parents else None

                    if not virtual_parent or not pruning_point:
                        _logger.warning("Missing virtual_parent/pruning_point in getBlockDagInfoResponse")
                        await asyncio.sleep(poll_seconds)
                        continue

                    cursor = start_from or await _vars_get(s, vars_key) or pruning_point

                    req = {"lowHash": cursor, "includeBlocks": True, "includeTransactions": True}
                    resp = await kaspad.request("getBlocksRequest", req, timeout=120)
                    resp = resp.get("getBlocksResponse") or {}

                    blocks = resp.get("blocks") or []
                    block_hashes = resp.get("blockHashes") or []

                    if not blocks:
                        await asyncio.sleep(poll_seconds)
                        continue

                    # Avoid reprocessing the cursor block when the response includes it.
                    if block_hashes and block_hashes[0] == cursor:
                        blocks = blocks[1:]
                        block_hashes = block_hashes[1:]

                    if not blocks:
                        await asyncio.sleep(poll_seconds)
                        continue

                    if batch_limit and len(blocks) > batch_limit:
                        blocks = blocks[:batch_limit]
                        block_hashes = block_hashes[:batch_limit]

                    last_hash = block_hashes[-1] if block_hashes else (blocks[-1].get("verboseData") or {}).get("hash")

                    # Local cache for resolving previous outpoints within this batch.
                    outputs_cache: dict[tuple[str, int], tuple[str | None, int | None, str | None]] = {}

                    for block in blocks:
                        header = block.get("header") or {}
                        verbose = block.get("verboseData") or {}

                        block_hash = verbose.get("hash")
                        if not block_hash:
                            continue

                        block_time = _as_int(header.get("timestamp"))
                        blue_score = _as_int(header.get("blueScore"))
                        daa_score = _as_int(header.get("daaScore"))
                        bits = _as_int(header.get("bits"))
                        version = _as_int(header.get("version"))

                        block_row = {
                            "hash": _norm_hex(block_hash),
                            "accepted_id_merkle_root": _norm_hex(header.get("acceptedIdMerkleRoot")),
                            "merge_set_blues_hashes": [
                                h for h in (_norm_hex(x) for x in (verbose.get("mergeSetBluesHashes") or [])) if h
                            ],
                            "merge_set_reds_hashes": [
                                h for h in (_norm_hex(x) for x in (verbose.get("mergeSetRedsHashes") or [])) if h
                            ],
                            "selected_parent_hash": _norm_hex(verbose.get("selectedParentHash")),
                            "bits": bits,
                            "blue_score": blue_score,
                            "blue_work": _norm_hex(header.get("blueWork")),
                            "daa_score": daa_score,
                            "hash_merkle_root": _norm_hex(header.get("hashMerkleRoot")),
                            "nonce": _as_int(header.get("nonce")) or 0,
                            "pruning_point": _norm_hex(header.get("pruningPoint")),
                            "timestamp": block_time,
                            "utxo_commitment": _norm_hex(header.get("utxoCommitment")),
                            "version": version,
                        }
                        if not block_row["hash"]:
                            continue

                        try:
                            await sb.execute(
                                insert(Block)
                                .values([block_row])
                                .on_conflict_do_nothing(index_elements=["hash"])
                            )
                        except sqlalchemy.exc.StatementError:
                            _logger.error("Block insert failed for %s", block_hash)
                            _debug_hex_field("blocks.hash", block_row.get("hash"))
                            _debug_hex_field("blocks.accepted_id_merkle_root", block_row.get("accepted_id_merkle_root"))
                            _debug_hex_field("blocks.selected_parent_hash", block_row.get("selected_parent_hash"))
                            _debug_hex_field("blocks.blue_work", block_row.get("blue_work"))
                            _debug_hex_field("blocks.hash_merkle_root", block_row.get("hash_merkle_root"))
                            _debug_hex_field("blocks.pruning_point", block_row.get("pruning_point"))
                            _debug_hex_field("blocks.utxo_commitment", block_row.get("utxo_commitment"))
                            for i, h in enumerate(block_row.get("merge_set_blues_hashes") or []):
                                _debug_hex_field(f"blocks.merge_set_blues_hashes[{i}]", h)
                            for i, h in enumerate(block_row.get("merge_set_reds_hashes") or []):
                                _debug_hex_field(f"blocks.merge_set_reds_hashes[{i}]", h)
                            raise

                        parents = header.get("parents") or []
                        parent_rows: list[dict[str, Any]] = []
                        for level in parents:
                            for ph in (level.get("parentHashes") or []):
                                parent_rows.append({"block_hash": _norm_hex(block_hash), "parent_hash": _norm_hex(ph)})
                        if parent_rows:
                            await sb.execute(
                                insert(BlockParent)
                                .values(parent_rows)
                                .on_conflict_do_nothing(index_elements=["block_hash", "parent_hash"])
                            )

                        tx_ids = verbose.get("transactionIds") or []
                        if tx_ids:
                            await sb.execute(
                                insert(BlockTransaction)
                                .values(
                                    [
                                        {
                                            "block_hash": _norm_hex(block_hash),
                                            "transaction_id": _norm_hex(txid),
                                        }
                                        for txid in tx_ids
                                        if _norm_hex(txid)
                                    ]
                                )
                                .on_conflict_do_nothing(index_elements=["block_hash", "transaction_id"])
                            )

                        is_chain_block = bool(verbose.get("isChainBlock"))

                        for tx in block.get("transactions") or []:
                            tx_verbose = tx.get("verboseData") or {}
                            txid = tx_verbose.get("transactionId")
                            if not txid:
                                continue

                            txid = _norm_hex(txid)
                            if not txid:
                                continue

                            subnetwork_str = tx.get("subnetworkId")
                            subnet_id = await _get_or_create_subnetwork_id(s, subnetwork_str)

                            await s.execute(
                                insert(Transaction)
                                .values(
                                    [
                                        {
                                            "transaction_id": txid,
                                            "subnetwork_id": subnet_id,
                                            "hash": _norm_hex(tx_verbose.get("hash")),
                                            "mass": _as_int(tx.get("mass")),
                                            "payload": _norm_hex(tx.get("payload")),
                                            "block_time": _as_int(tx_verbose.get("blockTime")) or block_time,
                                        }
                                    ]
                                )
                                .on_conflict_do_nothing(index_elements=["transaction_id"])
                            )

                            if is_chain_block:
                                await s.execute(
                                    insert(TransactionAcceptance)
                                    .values([{"transaction_id": txid, "block_hash": _norm_hex(block_hash)}])
                                    .on_conflict_do_nothing(index_elements=["transaction_id"])
                                )

                            # Outputs
                            out_rows: list[dict[str, Any]] = []
                            addr_map_rows: list[dict[str, Any]] = []
                            script_map_rows: list[dict[str, Any]] = []

                            for out_i, out in enumerate(tx.get("outputs") or []):
                                spk = (out.get("scriptPublicKey") or {}).get("scriptPublicKey")
                                amount = _as_int(out.get("amount"))
                                out_v = out.get("verboseData") or {}
                                addr = _qualify_address(out_v.get("scriptPublicKeyAddress"))

                                spk = _norm_hex(spk)

                                out_rows.append(
                                    {
                                        "transaction_id": txid,
                                        "index": out_i,
                                        "amount": amount,
                                        "script_public_key": spk,
                                        "script_public_key_address": addr,
                                    }
                                )

                                outputs_cache[(txid, out_i)] = (spk, amount, addr)

                                if USE_SCRIPT_FOR_ADDRESS:
                                    if spk:
                                        script_map_rows.append(
                                            {"transaction_id": txid, "script_public_key": spk, "block_time": block_time}
                                        )
                                else:
                                    if addr:
                                        addr_map_rows.append({"transaction_id": txid, "address": addr, "block_time": block_time})

                            if out_rows:
                                await s.execute(
                                    insert(TransactionOutput)
                                    .values(out_rows)
                                    .on_conflict_do_nothing(index_elements=["transaction_id", "index"])
                                )

                            # Inputs + resolve previous outpoints when possible
                            in_rows: list[dict[str, Any]] = []
                            for in_i, txin in enumerate(tx.get("inputs") or []):
                                prev = txin.get("previousOutpoint") or {}
                                prev_hash = prev.get("transactionId")
                                prev_index = _as_int(prev.get("index"))

                                prev_hash = _norm_hex(prev_hash)

                                prev_script = None
                                prev_amount = None
                                prev_addr = None

                                if prev_hash is not None and prev_index is not None:
                                    cached = outputs_cache.get((prev_hash, prev_index))
                                    if cached:
                                        prev_script, prev_amount, prev_addr = cached
                                    else:
                                        prev_script, prev_amount, prev_addr = await _fetch_prev_outpoint(s, prev_hash, prev_index)

                                prev_addr = _qualify_address(prev_addr)

                                in_rows.append(
                                    {
                                        "transaction_id": txid,
                                        "index": in_i,
                                        "previous_outpoint_hash": prev_hash,
                                        "previous_outpoint_index": prev_index,
                                        "signature_script": _norm_hex(txin.get("signatureScript")),
                                        "sig_op_count": _as_int(txin.get("sigOpCount")),
                                        "previous_outpoint_script": _norm_hex(prev_script),
                                        "previous_outpoint_amount": prev_amount,
                                    }
                                )

                                if USE_SCRIPT_FOR_ADDRESS:
                                    if prev_script:
                                        script_map_rows.append(
                                            {"transaction_id": txid, "script_public_key": prev_script, "block_time": block_time}
                                        )
                                else:
                                    if prev_addr:
                                        addr_map_rows.append({"transaction_id": txid, "address": prev_addr, "block_time": block_time})

                            if in_rows:
                                await s.execute(
                                    insert(TransactionInput)
                                    .values(in_rows)
                                    .on_conflict_do_nothing(index_elements=["transaction_id", "index"])
                                )

                            if USE_SCRIPT_FOR_ADDRESS:
                                if script_map_rows:
                                    await s.execute(
                                        insert(TxScriptMapping)
                                        .values(script_map_rows)
                                        .on_conflict_do_nothing(index_elements=["transaction_id", "script_public_key"])
                                    )
                            else:
                                if addr_map_rows:
                                    await s.execute(
                                        insert(TxAddrMapping)
                                        .values(addr_map_rows)
                                        .on_conflict_do_nothing(index_elements=["transaction_id", "address"])
                                    )

                    await _vars_set(s, vars_key, last_hash)
                    await _vars_set(s, "indexer_virtual_parent", str(virtual_parent))

                    await sb.commit()
                    await s.commit()

                except Exception:
                    _logger.exception("Indexer loop failed")

        await asyncio.sleep(poll_seconds)


def main() -> None:
    logging.basicConfig(
        format="%(asctime)s::%(levelname)s::%(name)s::%(message)s",
        level=logging.DEBUG if os.getenv("DEBUG") else logging.INFO,
        handlers=[logging.StreamHandler()],
    )

    asyncio.run(index_forever())


if __name__ == "__main__":
    main()
