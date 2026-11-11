"""Run a stand-in server on a free local port inside the test process (thread)."""
import socket
import threading
import time
from contextlib import contextmanager

import uvicorn

from standin.config import StandinConfig
from standin.server import create_app

FAST = dict(ttfr_median_ms=40.0, ttfr_sigma=0.1, ms_per_token=0.05, content_tail_ms_mean=5.0,
            tokens_out_median=60.0, tokens_out_sigma=0.1, hang_s=3.0)


def fast_cfg(**kw) -> StandinConfig:
    return StandinConfig().with_(**{**FAST, **kw})


@contextmanager
def running_standin(cfg: StandinConfig):
    s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
    server = uvicorn.Server(uvicorn.Config(create_app(cfg), host="127.0.0.1", port=port,
                                           log_level="error"))
    th = threading.Thread(target=server.run, daemon=True)
    th.start()
    for _ in range(200):
        if server.started:
            break
        time.sleep(0.02)
    try:
        yield f"http://127.0.0.1:{port}/openai/v1", f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        th.join(timeout=5)
