"""
Mining worker — one thread per CPU core.

Fast path: calls rx_batch_mine() in librxbatch.so — the entire nonce loop
runs in C with the GIL released, giving true multi-core parallelism and zero
per-hash Python overhead.

Fallback: pipelined hash_first/hash_next/hash_last if librxbatch.so is absent.

Nonce format: raw little-endian bytes hex (XMRig convention).
"""

import ctypes
import struct
import threading
import time
import logging
from typing import Optional

from randomx_hasher import RandomXVM, get_batch_lib
from stratum_client import StratumClient, StratumJob

logger = logging.getLogger(__name__)

_NONCE_OFF  = 39     # byte offset of the 4-byte nonce in a Monero blob
_BATCH_SIZE = 512    # hashes per C call — balances job-update latency vs overhead


class MiningWorker(threading.Thread):

    def __init__(self, worker_id: int, num_workers: int, client: StratumClient):
        super().__init__(daemon=True, name=f"worker-{worker_id}")
        self.worker_id  = worker_id
        self.num_workers = num_workers
        self.client     = client

        self._running   = False
        self._job: Optional[StratumJob] = None
        self._job_event = threading.Event()
        self._job_lock  = threading.Lock()

        self.hashes: int    = 0
        self._start_time: float = 0.0      # set on first hash (excludes build wait)

    def set_job(self, job: StratumJob):
        with self._job_lock:
            self._job = job
        self._job_event.set()

    def run(self):
        self._running = True
        vm = RandomXVM()
        batch_lib = get_batch_lib()

        if batch_lib:
            logger.info(f"Worker {self.worker_id}: using native C batch miner")
        else:
            logger.warning(
                f"Worker {self.worker_id}: librxbatch.so not found — "
                "using Python pipelined fallback (run build_randomx.sh to enable C batch mode)"
            )

        while self._running:
            self._job_event.wait(timeout=2)
            self._job_event.clear()

            with self._job_lock:
                job = self._job

            if job is None:
                continue

            if batch_lib:
                self._mine_batch(vm, job, batch_lib)
            else:
                self._mine_pipeline(vm, job)

        vm.close()

    # ------------------------------------------------------------------ #
    # Fast path: native C batch loop                                       #
    # ------------------------------------------------------------------ #

    def _mine_batch(self, vm: RandomXVM, job: StratumJob, lib):
        try:
            vm.ensure_seed(job.seed_hash)
        except Exception as exc:
            logger.error(f"Worker {self.worker_id} VM init failed: {exc}")
            return

        blob   = bytes(job.blob)                  # immutable — passed directly to C
        target = job.target_int                   # 64-bit expanded target
        stride = self.num_workers
        nonce  = ctypes.c_uint32(self.worker_id)

        out_nonce = ctypes.c_uint32(0xFFFFFFFF)
        out_hash  = ctypes.create_string_buffer(32)

        while self._running:
            # Check for a new job before each batch
            with self._job_lock:
                if self._job is not job:
                    return

            n_done = lib.rx_batch_mine(
                vm._vm,                            # randomx_vm*
                blob,                              # const uint8_t* blob_template
                len(blob),                         # blob_len
                _NONCE_OFF,                        # nonce_off
                nonce,                             # nonce_start
                ctypes.c_uint32(stride),           # nonce_stride
                _BATCH_SIZE,                       # batch_size
                ctypes.c_uint64(target),           # target
                ctypes.byref(out_nonce),           # *out_nonce
                out_hash,                          # out_hash[32]
            )
            if not self._start_time:
                self._start_time = time.monotonic()
            self.hashes += n_done

            if out_nonce.value != 0xFFFFFFFF:
                winning_nonce = out_nonce.value
                h = bytes(out_hash)
                self._submit(job, winning_nonce, h)
                out_nonce.value = 0xFFFFFFFF       # reset for next batch

            # Advance nonce past the batch
            nonce = ctypes.c_uint32(nonce.value + stride * _BATCH_SIZE)

    # ------------------------------------------------------------------ #
    # Fallback: Python pipelined loop                                      #
    # ------------------------------------------------------------------ #

    _STALE_CHECK = 64

    def _mine_pipeline(self, vm: RandomXVM, job: StratumJob):
        try:
            vm.ensure_seed(job.seed_hash)
        except Exception as exc:
            logger.error(f"Worker {self.worker_id} VM init failed: {exc}")
            return

        blob      = bytearray(job.blob)
        stride    = self.num_workers
        nonce     = self.worker_id & 0xFFFFFFFF
        pack_into = struct.pack_into

        pack_into("<I", blob, _NONCE_OFF, nonce)
        vm.hash_first(bytes(blob))
        prev_nonce = nonce
        nonce      = (nonce + stride) & 0xFFFFFFFF
        batch      = 0

        meets_target = job.meets_target
        hash_next    = vm.hash_next
        hash_last    = vm.hash_last

        while self._running:
            if batch >= self._STALE_CHECK:
                batch = 0
                with self._job_lock:
                    if self._job is not job:
                        h = hash_last()
                        self.hashes += 1
                        if meets_target(h):
                            self._submit(job, prev_nonce, h)
                        return

            pack_into("<I", blob, _NONCE_OFF, nonce)
            h = hash_next(bytes(blob))
            if not self._start_time:
                self._start_time = time.monotonic()
            self.hashes += 1
            batch += 1

            if meets_target(h):
                self._submit(job, prev_nonce, h)

            prev_nonce = nonce
            nonce      = (nonce + stride) & 0xFFFFFFFF

        h = hash_last()
        self.hashes += 1
        if meets_target(h):
            self._submit(job, prev_nonce, h)

    # ------------------------------------------------------------------ #
    # Shared helpers                                                       #
    # ------------------------------------------------------------------ #

    def _submit(self, job: StratumJob, nonce: int, h: bytes):
        nonce_hex  = struct.pack("<I", nonce).hex()
        result_hex = h.hex()
        logger.info(
            f"Worker {self.worker_id} found share! "
            f"nonce={nonce_hex} hash={result_hex[:16]}…"
        )
        self.client.submit_share(job.job_id, nonce_hex, result_hex)

    def stop(self):
        self._running = False

    @property
    def hashrate(self) -> float:
        elapsed = time.monotonic() - self._start_time
        return self.hashes / elapsed if elapsed > 0 else 0.0
