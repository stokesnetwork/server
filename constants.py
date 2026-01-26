import os

KASPAD_WRPC_URL = os.getenv("KASPAD_WRPC_URL")

USE_SCRIPT_FOR_ADDRESS = os.getenv("USE_SCRIPT_FOR_ADDRESS", "false").lower() == "true"
PREV_OUT_RESOLVED = os.getenv("PREV_OUT_RESOLVED", "false").lower() == "true"

TX_SEARCH_ID_LIMIT = int(os.getenv("TX_SEARCH_ID_LIMIT", "1_000"))
TX_SEARCH_BS_LIMIT = int(os.getenv("TX_SEARCH_BS_LIMIT", "100"))
HEALTH_TOLERANCE_DOWN = int(os.getenv("HEALTH_TOLERANCE_DOWN", "300"))

HASHRATE_HISTORY = os.getenv("HASHRATE_HISTORY", "false").lower() == "true"
ADDRESS_RANKINGS = os.getenv("ADDRESS_RANKINGS", "false").lower() == "true"

NETWORK_TYPE = os.getenv("NETWORK_TYPE", "mainnet").lower()
BPS = int(os.getenv("BPS", "1"))

if NETWORK_TYPE in {"stokes-mainnet", "stokes_mainnet"}:
    NETWORK_TYPE = "mainnet"
elif NETWORK_TYPE in {"stokes-testnet", "stokes_testnet"}:
    NETWORK_TYPE = "testnet"
elif NETWORK_TYPE in {"stokes-simnet", "stokes_simnet"}:
    NETWORK_TYPE = "simnet"
elif NETWORK_TYPE in {"stokes-devnet", "stokes_devnet"}:
    NETWORK_TYPE = "devnet"

match NETWORK_TYPE:
    case "mainnet":
        address_prefix = "stokes"
        address_example = "stokes:qqcxjzhcxcqmtf9t8ed2lpkfjfse79q9ncafluc3st6znkhpl0g92ck3gd2z2"
    case "testnet":
        address_prefix = "stokestest"
        address_example = "stokestest:qpkqllexmwjpaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    case "simnet":
        address_prefix = "stokessim"
        address_example = "stokessim:qpkqllexmwjpaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    case "devnet":
        address_prefix = "stokesdev"
        address_example = "stokesdev:qpkqllexmwjpaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    case _:
        raise ValueError(f"Network type {NETWORK_TYPE} not supported.")

ADDRESS_PREFIX = address_prefix
ADDRESS_EXAMPLE = address_example

REGEX_KASPA_ADDRESS = "^" + ADDRESS_PREFIX + ":[a-z0-9]{61,63}$"
