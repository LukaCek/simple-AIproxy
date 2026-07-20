import argparse
import asyncio
import time
from typing import Optional

import main


async def run_worker(worker_id: str, interval_seconds: float, once: bool) -> int:
    main.init_database()
    main.config_data = main.load_config()
    main.http_client = main.httpx.AsyncClient(
        timeout=main.httpx.Timeout(main.UPSTREAM_REQUEST_TIMEOUT_SECONDS, connect=10.0),
        limits=main.httpx.Limits(max_keepalive_connections=10, max_connections=20),
    )
    try:
        while True:
            result: Optional[dict] = await main.execute_background_job_once(worker_id=worker_id)
            if result is not None:
                print(f"{result['id']} {result['status']}", flush=True)
                if once:
                    return 0
            elif once:
                print("no queued job", flush=True)
                return 0
            else:
                time.sleep(interval_seconds)
    finally:
        if main.http_client is not None:
            await main.http_client.aclose()
            main.http_client = None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run simple-AIproxy slow-brain background worker hooks")
    parser.add_argument("--worker-id", default="slowbrain-worker", help="worker id stored on leased jobs")
    parser.add_argument("--interval-seconds", type=float, default=2.0, help="poll delay when no jobs are available")
    parser.add_argument("--once", action="store_true", help="lease and execute at most one job")
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    raise SystemExit(asyncio.run(run_worker(args.worker_id, args.interval_seconds, args.once)))
