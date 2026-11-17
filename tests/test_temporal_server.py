import pytest

from tests.temporal_server import start_with_retry


async def test_a_failed_start_is_tried_again_in_a_fresh_directory():
    dirs: list[str] = []

    async def start(download_dir: str) -> str:
        dirs.append(download_dir)
        if len(dirs) < 3:
            raise RuntimeError("Failed starting test server: could not extract/read tar archive entry")
        return "started"

    assert await start_with_retry(start, attempts=3, pause=0) == "started"
    assert len(set(dirs)) == 3


async def test_the_last_failure_is_raised():
    calls = 0

    async def start(download_dir: str) -> str:
        nonlocal calls
        calls += 1
        raise RuntimeError("still broken")

    with pytest.raises(RuntimeError, match="still broken"):
        await start_with_retry(start, attempts=2, pause=0)
    assert calls == 2
