"""Displayfd is a newline-framed protocol, not a single pipe read."""
import asyncio
import os

import pytest

from scripts.rpg_benchmark import renderer


def test_display_number_waits_for_terminator_even_after_all_digits_arrive():
    async def probe():
        read_fd, write_fd = os.pipe()
        try:
            task = asyncio.create_task(renderer.read_display_number(read_fd))
            os.write(write_fd, b"123")
            # Let the reader consume the digits before publishing the terminator.
            await asyncio.sleep(.02)
            assert not task.done(), "closing here can EPIPE Xvfb's separate newline write"
            os.write(write_fd, b"\n")
            assert await task == ":123"
        finally:
            os.close(read_fd)
            os.close(write_fd)
    asyncio.run(probe())


@pytest.mark.parametrize("payload", [b"", b"12", b"\n", b":12\n", b"12x\n", b"1" * 65])
def test_display_number_rejects_eof_malformed_or_overlong_frame(payload):
    async def probe():
        read_fd, write_fd = os.pipe()
        try:
            os.write(write_fd, payload)
            os.close(write_fd)
            with pytest.raises(RuntimeError, match="display"):
                await renderer.read_display_number(read_fd)
        finally:
            os.close(read_fd)
    asyncio.run(probe())


def test_display_reader_cancellation_unregisters_fd():
    async def probe():
        read_fd, write_fd = os.pipe()
        try:
            task = asyncio.create_task(renderer.read_display_number(read_fd))
            await asyncio.sleep(0)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert not asyncio.get_running_loop().remove_reader(read_fd)
            os.write(write_fd, b"7\n")  # No background reader stole the next frame.
            assert os.read(read_fd, 2) == b"7\n"
        finally:
            os.close(read_fd)
            os.close(write_fd)
    asyncio.run(probe())
