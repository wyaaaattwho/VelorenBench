#!/usr/bin/env python3
"""Transparent stdio client for the episode's existing native actor."""
from __future__ import annotations

import asyncio
import os
import sys


async def relay() -> None:
    reader, writer = await asyncio.open_unix_connection(
        os.environ["RPG_EPISODE_SOCKET"], limit=16 * 1024 * 1024)

    async def requests() -> None:
        loop = asyncio.get_running_loop()
        stdin = asyncio.StreamReader(limit=16 * 1024 * 1024)
        await loop.connect_read_pipe(lambda: asyncio.StreamReaderProtocol(stdin), sys.stdin.buffer)
        while line := await stdin.readline():
            writer.write(line)
            await writer.drain()
        writer.write_eof()

    async def responses() -> None:
        while line := await reader.readline():
            sys.stdout.buffer.write(line)
            sys.stdout.buffer.flush()

    sending = asyncio.create_task(requests())
    try:
        await responses()
    finally:
        sending.cancel()
        await asyncio.gather(sending, return_exceptions=True)
        writer.close()
        await writer.wait_closed()


def main() -> None:
    asyncio.run(relay())


if __name__ == "__main__":
    main()
