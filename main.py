#!/usr/bin/env python3
"""
py-randomx-miner
================
A multi-threaded Python miner for Monero (XMR) and other RandomX coins.

Usage:
    python3 main.py [--config config.json]

Edit config.json to set your pool, wallet address, and thread count.
"""

import argparse
import json
import logging
import os
import signal
import sys
import time
import threading
from typing import List

from stratum_client import StratumClient, StratumJob
from worker import MiningWorker

# ------------------------------------------------------------------ #
# Logging                                                              #
# ------------------------------------------------------------------ #

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("miner")


# ------------------------------------------------------------------ #
# Config                                                               #
# ------------------------------------------------------------------ #

DEFAULT_CONFIG = {
    "pool": {"host": "pool.supportxmr.com", "port": 3333, "tls": False},
    "wallet": "YOUR_MONERO_WALLET_ADDRESS",
    "worker": "worker1",
    "password": "x",
    "threads": 2,
    "log_interval_seconds": 10,
}


def load_config(path: str) -> dict:
    if os.path.exists(path):
        with open(path) as f:
            cfg = json.load(f)
        # Merge with defaults for any missing keys
        for k, v in DEFAULT_CONFIG.items():
            cfg.setdefault(k, v)
        return cfg
    logger.warning(f"Config file {path!r} not found — using defaults")
    return DEFAULT_CONFIG.copy()


# ------------------------------------------------------------------ #
# Miner orchestrator                                                   #
# ------------------------------------------------------------------ #

class Miner:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.workers: List[MiningWorker] = []
        self._running = False

        n_threads = max(1, cfg.get("threads", 2))
        pool = cfg["pool"]
        wallet = cfg["wallet"]

        if wallet == "YOUR_MONERO_WALLET_ADDRESS":
            logger.error(
                "Please set your Monero wallet address in config.json before mining!"
            )
            sys.exit(1)

        self.client = StratumClient(
            host=pool["host"],
            port=int(pool["port"]),
            wallet=wallet,
            worker=cfg.get("worker", "worker1"),
            password=cfg.get("password", "x"),
            use_tls=bool(pool.get("tls", False)),
            on_job=self._on_new_job,
        )

        for i in range(n_threads):
            self.workers.append(MiningWorker(i, n_threads, self.client))

        logger.info(
            f"py-randomx-miner starting — "
            f"pool={pool['host']}:{pool['port']} "
            f"threads={n_threads} "
            f"worker={cfg.get('worker', 'worker1')}"
        )

    def _on_new_job(self, job: StratumJob):
        for w in self.workers:
            w.set_job(job)

    def start(self):
        self._running = True
        for w in self.workers:
            w.start()
        self.client.start()

        interval = self.cfg.get("log_interval_seconds", 10)
        self._stats_thread = threading.Thread(
            target=self._stats_loop, args=(interval,), daemon=True
        )
        self._stats_thread.start()

    def stop(self):
        self._running = False
        logger.info("Shutting down…")
        self.client.stop()
        for w in self.workers:
            w.stop()

    def _stats_loop(self, interval: int):
        while self._running:
            time.sleep(interval)
            total_hr = sum(w.hashrate for w in self.workers)
            total_hashes = sum(w.hashes for w in self.workers)
            per_thread = [f"T{w.worker_id}:{w.hashrate:.1f}" for w in self.workers]
            logger.info(
                f"Hashrate: {total_hr:.2f} H/s  "
                f"({', '.join(per_thread)})  "
                f"Total hashes: {total_hashes:,}  "
                f"Shares: {self.client.accepted} accepted / {self.client.rejected} rejected"
            )


# ------------------------------------------------------------------ #
# Entry point                                                          #
# ------------------------------------------------------------------ #

def main():
    parser = argparse.ArgumentParser(description="py-randomx-miner — Monero miner")
    parser.add_argument(
        "--config",
        default=os.path.join(os.path.dirname(__file__), "config.json"),
        help="Path to config.json (default: config.json next to main.py)",
    )
    args = parser.parse_args()

    cfg = load_config(args.config)
    miner = Miner(cfg)

    def _shutdown(sig, frame):
        miner.stop()
        sys.exit(0)

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    miner.start()

    # Keep main thread alive
    while True:
        time.sleep(1)


if __name__ == "__main__":
    main()
