# encoding: utf-8
from asyncio import wait_for

from fastapi import Path, HTTPException
from pydantic import BaseModel

from constants import ADDRESS_EXAMPLE, REGEX_KASPA_ADDRESS
from kaspad.KaspadRpcClient import kaspad_rpc_client
from server import app, kaspad_client


class BalanceResponse(BaseModel):
    address: str = ADDRESS_EXAMPLE
    balance: int = 38240000000


async def _get_balance_for_address(address: str):
    rpc_client = await kaspad_rpc_client()
    request = {"address": address}
    if rpc_client:
        balance = await wait_for(rpc_client.get_balance_by_address(request), 10)
    else:
        resp = await kaspad_client.request("getBalanceByAddressRequest", request)
        if resp.get("error"):
            raise HTTPException(500, resp["error"])
        balance = resp["getBalanceByAddressResponse"]

    return {"address": address, "balance": balance["balance"]}


@app.get("/addresses/{stokesAddress}/balance", response_model=BalanceResponse, tags=["Stokes addresses"])
async def get_balance_from_stokes_address(
    stokes_address: str = Path(
        alias="stokesAddress", description=f"Stokes address as string e.g. {ADDRESS_EXAMPLE}", regex=REGEX_KASPA_ADDRESS
    ),
):
    """
    Get balance for a given Stokes address
    """
    return await _get_balance_for_address(stokes_address)


@app.get(
    "/addresses/{kaspaAddress}/balance",
    response_model=BalanceResponse,
    tags=["Stokes addresses"],
    include_in_schema=False,
)
async def get_balance_from_kaspa_address(
    kaspaAddress: str = Path(description=f"Stokes address as string e.g. {ADDRESS_EXAMPLE}", regex=REGEX_KASPA_ADDRESS),
):
    return await _get_balance_for_address(kaspaAddress)
