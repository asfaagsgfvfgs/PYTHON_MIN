"""
Mining worker — one thread per CPU core.

Each worker:
  1. Keeps its own RandomX VM (cache ~256 MB in light mode)
  2. Iterates nonces from its dedicated stride
  3. Submits shares via the shared StratumClient

Nonce format: submitted as raw little-endian bytes hex (e.g. nonce 0xAD0
is stored as d0 0a 00 00 in the blob and submitted as "d00a0000").
This matches the XMRig convention that all major Monero pools expect.
"""

import struct
import threading
import time
import logging
from typing import Optional

from randomx_hasher import RandomXVM
from stratum_client import StratumClient, StratumJob

logger = logging.getLogger(__name__)

_STALE_CHECK_INTERVAL = 64  # check for new job every N hashes


class MiningWorker(threading.Thread):

    def __init__(self, worker_id: int, num_workers: int, client: StratumClient):
        super().__init__(daemon=True, name=f"worker-{worker_id}")
        self.worker_id = worker_id
        self.num_workers = num_workers
        self.client = client

        self._running = False
        self._job: Optional[StratumJob] = None
        self._job_event = threading.Event()
        self._job_lock = threading.Lock()

        self.hashes: int = 0
        self._start_time: float = 0.0

    def set_job(self, job: StratumJob):
        with self._job_lock:
            self._job = job
        self._job_event.set()

    def run(self):
        self._running = True
        self._start_time = time.monotonic()
        vm = RandomXVM(light_mode=True)
        logger.debug(f"Worker {self.worker_id} started")

        while self._running:
            self._job_event.wait(timeout=2)
            self._job_event.clear()

            with self._job_lock:
                job = self._job

            if job is None:
                continue

            self._mine(vm, job)

        vm.close()

    def _mine(self, vm: RandomXVM, job: StratumJob):
        try:
            vm.ensure_seed(job.seed_hash)
        except Exception as exc:
            logger.error(f"Worker {self.worker_id} VM init failed: {exc}")
            return

        blob = bytearray(job.blob)
        nonce_offset = 39   # 4-byte LE nonce position in Monero blob
        stride = self.num_workers
        nonce = self.worker_id & 0xFFFFFFFF
        batch = 0

        while self._running:
            # Check for new job every _STALE_CHECK_INTERVAL hashes
            if batch >= _STALE_CHECK_INTERVAL:
                batch = 0
                with self._job_lock:
                    if self._job is not job:
                        return  # stale — outer loop will pick up new job

            # Write nonce as little-endian into blob
            struct.pack_into("<I", blob, nonce_offset, nonce)

            h = vm.hash(bytes(blob))
            self.hashes += 1
            batch += 1

            if job.meets_target(h):
                # Submit nonce as raw LE bytes hex — this is what the pool
                # writes directly into the blob template when verifying.
                nonce_hex = struct.pack("<I", nonce).hex()
                result_hex = h.hex()
                logger.info(
                    f"Worker {self.worker_id} found share! "
                    f"nonce={nonce_hex} hash={result_hex[:16]}…"
                )
                self.client.submit_share(job.job_id, nonce_hex, result_hex)

            nonce = (nonce + stride) & 0xFFFFFFFF

    def stop(self):
        self._running = False

    @property
    def hashrate(self) -> float:
        elapsed = time.monotonic() - self._start_time
        return self.hashes / elapsed if elapsed > 0 else 0.0
