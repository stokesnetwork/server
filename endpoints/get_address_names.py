# encoding: utf-8
from typing import List

from fastapi import Path, HTTPException
from kaspa_script_address import to_script
from pydantic import BaseModel
from sqlalchemy.future import select
from starlette.responses import Response

from constants import REGEX_KASPA_ADDRESS
from dbsession import async_session
from endpoints import sql_db_only
from models.AddressKnown import AddressKnown
from server import app


class AddressName(BaseModel):
    address: str
    name: str


@app.get(
    "/addresses/names",
    response_model=List[AddressName],
    tags=["Stokes addresses"],
    openapi_extra={"strict_query_params": True},
)
@sql_db_only
async def get_addresses_names(response: Response):
    """
    Get the name for an address
    """
    response.headers["Cache-Control"] = "public, max-age=60"
    async with async_session() as s:
        rows = (await s.execute(select(AddressKnown))).scalars().all()
        return [{"name": r.name, "address": r.address} for r in rows]


@app.get(
    "/addresses/{kaspaAddress}/name",
    response_model=AddressName | None,
    tags=["Stokes addresses"],
    openapi_extra={"strict_query_params": True},
)
@sql_db_only
async def get_name_for_address(
    response: Response,
    kaspa_address: str = Path(
        alias="kaspaAddress",
        description="Stokes address as string e.g. stokes:qqcxjzhcxcqmtf9t8ed2lpkfjfse79q9ncafluc3st6znkhpl0g92ck3gd2z2",
        regex=REGEX_KASPA_ADDRESS,
    ),
):
    """
    Get the name for an address
    """
    try:
        to_script(kaspa_address)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Invalid address: {kaspa_address}")

    async with async_session() as s:
        r = (await s.execute(select(AddressKnown).filter(AddressKnown.address == kaspa_address))).first()

    response.headers["Cache-Control"] = "public, max-age=600"
    if r:
        return AddressName(address=r.AddressKnown.address, name=r.AddressKnown.name)
    else:
        raise HTTPException(
            status_code=404, detail="Address name not found", headers={"Cache-Control": "public, max-age=600"}
        )
