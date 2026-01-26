# stokes-server

REST API server for Stokes written in Python.

The rest server is designed to operate on the database populated by an indexer (Postgres).

## Build and run

Any third party integrator which depends on the api should make sure to run their own instance.

### Docker

The easiest way to get going is using the pre-built Docker images. Check out the Docker compose example [here](https://github.com/supertypo/simply-kaspa-indexer).

### From source

You will need to install Python (3.12) as well as poetry first.  
Then:

```shell
poetry install --no-root --no-interaction
export DEBUG=true
export KASPAD_HOST_1=localhost:17210
export SQL_URI=postgresql+asyncpg://postgres:postgres@localhost:5432/postgres
poetry run gunicorn -b 0.0.0.0:8000 -w 4 -k uvicorn.workers.UvicornWorker main:app
```

### Testnet run (no local node required)

You can run stokes-server against any reachable Stokes testnet node. This is the recommended way to validate explorer/API behavior before mainnet.

Example (point at a remote testnet node):

```shell
export NETWORK_TYPE=testnet
export BPS=1
export DISABLE_PRICE=true
export DEBUG=true
export KASPAD_HOST_1=95.216.155.253:17210

poetry run uvicorn main:app --host 0.0.0.0 --port 8000 --log-level info
```

Example (network-wide explorer mode with multiple remote nodes / failover):

```shell
export NETWORK_TYPE=testnet
export BPS=1
export DISABLE_PRICE=true
export DEBUG=true

export KASPAD_HOST_1=95.216.155.253:17210
export KASPAD_HOST_2=157.90.151.188:17210
export KASPAD_HOST_3=138.199.233.192:17210

poetry run uvicorn main:app --host 0.0.0.0 --port 8000 --log-level info
```

### Mainnet run (go-live)

Same as above, but point to your mainnet node(s) and set:

```shell
export NETWORK_TYPE=mainnet
export BPS=1
export DISABLE_PRICE=false
export KASPAD_HOST_1=<your-mainnet-node>:17110
```

### Socket.IO live feed (kaspa-explorer compatible)

This server exposes a Socket.IO endpoint at:

`/ws/socket.io`

Events:

- `last-blocks` (client emits; server responds with a list). This requires the Postgres blocks DB to be populated.

  Note: if the Postgres blocks DB is not available or empty, `last-blocks` falls back to querying recent blocks via RPC from the configured node(s). In fallback mode, `txCount` will be `0`.

- `join-room` with room names:
  - `bluescore`
  - `mempool`
  - `blocks`
- Server emits:
  - `bluescore` payload: `{ "blueScore": <int> }`
  - `mempool` payload: `<int>`
  - `new-block` payload: block object (at minimum includes `header.hash`)

Minimal sanity client:

```shell
poetry run python - <<'PY'
import asyncio, socketio

async def main():
    sio = socketio.AsyncClient(logger=False, engineio_logger=False)

    @sio.event
    async def connect():
        print("connected", sio.sid)
        await sio.emit("last-blocks", "")
        await sio.emit("join-room", "bluescore")
        await sio.emit("join-room", "mempool")
        await sio.emit("join-room", "blocks")

    @sio.on("bluescore")
    async def on_bluescore(data):
        print("bluescore", data)

    @sio.on("mempool")
    async def on_mempool(data):
        print("mempool", data)

    @sio.on("new-block")
    async def on_new_block(data):
        h = (data.get("header") or {}).get("hash") if isinstance(data, dict) else None
        print("new-block", h)

    await sio.connect(
        "http://127.0.0.1:8000",
        socketio_path="/ws/socket.io",
        transports=["polling"],
    )

    await asyncio.sleep(8)
    await sio.disconnect()
    print("disconnected")

asyncio.run(main())
PY
```

Note: `disconnected` is expected in this script because it intentionally disconnects after 8 seconds.

### Environment variables

- KASPAD_WRPC_URL - ws(s)://host:port (wrpc) to a Stokes node. (default: none)
- KASPAD_HOST_1 - host:port (grpc) to a Stokes node, multiple nodes are supported via KASPAD_HOST_2, KASPAD_HOST_3, ... (default: none)
- STOKESD_HOST - alias for KASPAD_HOST_1. (default: none)
- STOKESD_HOST_1 - alias for KASPAD_HOST_1. (default: none)
- SQL_URI - uri to a postgres db (default: postgresql+asyncpg://127.0.0.1:5432)
- SQL_URI_BLOCKS - uri to a postgres db to query for blocks, block_parent and blocks_transactions (default: SQL_URI)
- SQL_POOL_SIZE - postgres db pool size (default: 15)
- SQL_POOL_MAX_OVERFLOW - postgres db pool max overflow (default: 0)
- SQL_POOL_RECYCLE_SECONDS - postgres db connection ttl (default: 1200)
- HEALTH_TOLERANCE_DOWN - How many seconds behind kaspad the db can be before /info/health reports DOWN (default: 300)
- NETWORK_TYPE - mainnet/testnet/simnet/devnet (default: mainnet)
- BPS - Blocks per second, affects block difficulty/hashrate calculation (default: 1)
- DISABLE_PRICE - If true /info/price and /info/market-data is disabled (default: false)
- USE_SCRIPT_FOR_ADDRESS - If true scripts_transactions will be used for address to tx, see indexer doc (default: false)
- PREV_OUT_RESOLVED - If true tx inputs are assumed populated with sender address, see indexer doc (default: false)
- TX_SEARCH_ID_LIMIT - adjust the maximum number of transactionIds for transactions/search (default: 1000)
- TX_SEARCH_BS_LIMIT - adjust the maximum blue score range for transactions/search (default: 100)
- VSPC_REQUEST - If true enables /info/get-vscp-from-block (default: false)
- HASHRATE_HISTORY - If true populates hashrate_history table and enables /info/hashrate/history (default: false)
- ADDRESS_RANKINGS - If true enables /addresses/top,distribution. Requires UTXO exporter. (default: false)
- DEBUG - Enables additional logging (default: false)
