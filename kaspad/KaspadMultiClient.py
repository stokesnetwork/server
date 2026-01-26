# encoding: utf-8
import asyncio

from kaspad.KaspadClient import KaspadClient

# poetry run python -m grpc_tools.protoc -I./protos --python_out=. --grpc_python_out=. ./protos/rpc.proto ./protos/messages.proto
from kaspad.KaspadThread import KaspadCommunicationError


class KaspadMultiClient(object):
    def __init__(self, hosts: list[str]):
        self.kaspads = [KaspadClient(*h.split(":")) for h in hosts]

    def __get_kaspad(self):
        if not self.kaspads:
            return None

        # Prefer a fully synced + UTXO indexed node when available.
        for k in self.kaspads:
            if k.is_utxo_indexed and k.is_synced:
                return k

        # Fallback: return the first configured node so endpoints can still
        # serve data (and fail gracefully) while nodes are syncing.
        return self.kaspads[0]

    async def initialize_all(self):
        tasks = [asyncio.create_task(k.ping()) for k in self.kaspads]

        for t in tasks:
            await t

    async def request(self, command, params=None, timeout=5):
        try:
            kaspad = self.__get_kaspad()
            if kaspad is None:
                raise KaspadCommunicationError("No KASPAD hosts configured")
            return await kaspad.request(command, params, timeout=timeout)
        except KaspadCommunicationError:
            await self.initialize_all()
            kaspad = self.__get_kaspad()
            if kaspad is None:
                raise
            return await kaspad.request(command, params, timeout=timeout)

    async def notify(self, command, params, callback):
        kaspad = self.__get_kaspad()
        if kaspad is None:
            raise KaspadCommunicationError("No KASPAD hosts configured")
        return await kaspad.notify(command, params, callback)
