"""
Python ctypes wrapper around librandomx.so

Loads the shared library built from https://github.com/tevador/RandomX
and exposes the minimal surface needed for mining:
  - create / destroy VM
  - calculate hash (single and pipelined)
  - shared full-dataset (fast mode, ~2.4 GB RAM, 5-10x vs light mode)
"""

import ctypes
import ctypes.util
import os
import logging
import threading
import time

logger = logging.getLogger(__name__)

# Flags (from randomx_flags enum in randomx.h)
RANDOMX_FLAG_DEFAULT      = 0
RANDOMX_FLAG_LARGE_PAGES  = 1
RANDOMX_FLAG_HARD_AES     = 2
RANDOMX_FLAG_FULL_MEM     = 4
RANDOMX_FLAG_JIT          = 8
RANDOMX_FLAG_SECURE       = 16
RANDOMX_FLAG_ARGON2_SSSE3 = 32
RANDOMX_FLAG_ARGON2_AVX2  = 64
RANDOMX_FLAG_ARGON2       = 96

# How many threads to use when initialising the full dataset.
# Set this from main.py before any VM is created.
num_dataset_threads: int = 1


def _load_library() -> ctypes.CDLL:
    """Locate and load librandomx, searching common paths."""
    search_paths = [
        os.path.join(os.path.dirname(__file__), "RandomX", "build", "librandomx.so"),
        os.path.join(os.path.dirname(__file__), "librandomx.so"),
        "/usr/local/lib/librandomx.so",
        "/usr/lib/librandomx.so",
    ]
    for path in search_paths:
        if os.path.exists(path):
            logger.debug(f"Loading librandomx from {path}")
            return ctypes.CDLL(path)

    system = ctypes.util.find_library("randomx")
    if system:
        return ctypes.CDLL(system)

    raise FileNotFoundError(
        "librandomx.so not found.\n"
        "Build it with these commands (run from the folder where your .py files are):\n"
        "  git clone --depth=1 https://github.com/tevador/RandomX.git RandomX\n"
        "  mkdir -p RandomX/build && cd RandomX/build\n"
        "  cmake .. -DCMAKE_BUILD_TYPE=Release -DBUILD_SHARED_LIBS=ON\n"
        "  make -j$(nproc)\n"
        f"Expected location: {os.path.join(os.path.dirname(__file__), 'RandomX', 'build', 'librandomx.so')}"
    )


class _LibRandomX:
    """Thin ctypes wrapper — one instance shared across all workers."""

    def __init__(self):
        self._lib = _load_library()
        self._setup_signatures()

    def _setup_signatures(self):
        lib = self._lib

        lib.randomx_get_flags.restype = ctypes.c_int
        lib.randomx_get_flags.argtypes = []

        lib.randomx_alloc_cache.restype = ctypes.c_void_p
        lib.randomx_alloc_cache.argtypes = [ctypes.c_int]

        lib.randomx_init_cache.restype = None
        lib.randomx_init_cache.argtypes = [
            ctypes.c_void_p,
            ctypes.c_char_p,
            ctypes.c_size_t,
        ]

        lib.randomx_release_cache.restype = None
        lib.randomx_release_cache.argtypes = [ctypes.c_void_p]

        lib.randomx_alloc_dataset.restype = ctypes.c_void_p
        lib.randomx_alloc_dataset.argtypes = [ctypes.c_int]

        lib.randomx_dataset_item_count.restype = ctypes.c_ulong
        lib.randomx_dataset_item_count.argtypes = []

        lib.randomx_init_dataset.restype = None
        lib.randomx_init_dataset.argtypes = [
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_ulong,
            ctypes.c_ulong,
        ]

        lib.randomx_release_dataset.restype = None
        lib.randomx_release_dataset.argtypes = [ctypes.c_void_p]

        lib.randomx_create_vm.restype = ctypes.c_void_p
        lib.randomx_create_vm.argtypes = [
            ctypes.c_int,
            ctypes.c_void_p,
            ctypes.c_void_p,
        ]

        lib.randomx_destroy_vm.restype = None
        lib.randomx_destroy_vm.argtypes = [ctypes.c_void_p]

        lib.randomx_calculate_hash.restype = None
        lib.randomx_calculate_hash.argtypes = [
            ctypes.c_void_p,
            ctypes.c_char_p,
            ctypes.c_size_t,
            ctypes.c_char_p,
        ]

        lib.randomx_calculate_hash_first.restype = None
        lib.randomx_calculate_hash_first.argtypes = [
            ctypes.c_void_p,
            ctypes.c_char_p,
            ctypes.c_size_t,
        ]

        lib.randomx_calculate_hash_next.restype = None
        lib.randomx_calculate_hash_next.argtypes = [
            ctypes.c_void_p,
            ctypes.c_char_p,
            ctypes.c_size_t,
            ctypes.c_char_p,
        ]

        lib.randomx_calculate_hash_last.restype = None
        lib.randomx_calculate_hash_last.argtypes = [
            ctypes.c_void_p,
            ctypes.c_char_p,
        ]

    def get_flags(self, light_mode: bool = True) -> int:
        base = self._lib.randomx_get_flags()
        if light_mode:
            base &= ~RANDOMX_FLAG_FULL_MEM
        return base

    def alloc_cache(self, flags: int) -> int:
        ptr = self._lib.randomx_alloc_cache(flags)
        if not ptr:
            raise MemoryError("randomx_alloc_cache returned NULL")
        return ptr

    def init_cache(self, cache: int, key: bytes):
        self._lib.randomx_init_cache(cache, key, len(key))

    def release_cache(self, cache: int):
        self._lib.randomx_release_cache(cache)

    def create_vm(self, flags: int, cache: int, dataset: int = 0) -> int:
        vm = self._lib.randomx_create_vm(flags, cache, dataset or None)
        if not vm:
            raise MemoryError("randomx_create_vm returned NULL")
        return vm

    def destroy_vm(self, vm: int):
        self._lib.randomx_destroy_vm(vm)

    def calculate_hash(self, vm: int, data: bytes, out: ctypes.Array) -> None:
        self._lib.randomx_calculate_hash(vm, data, len(data), out)

    def hash_first(self, vm: int, data: bytes) -> None:
        self._lib.randomx_calculate_hash_first(vm, data, len(data))

    def hash_next(self, vm: int, data: bytes, out: ctypes.Array) -> None:
        self._lib.randomx_calculate_hash_next(vm, data, len(data), out)

    def hash_last(self, vm: int, out: ctypes.Array) -> None:
        self._lib.randomx_calculate_hash_last(vm, out)


# ------------------------------------------------------------------ #
# Module-level singletons                                              #
# ------------------------------------------------------------------ #

_rx_lock = threading.Lock()
_rx_instance: "_LibRandomX | None" = None


def get_rx() -> _LibRandomX:
    global _rx_instance
    if _rx_instance is None:
        with _rx_lock:
            if _rx_instance is None:
                _rx_instance = _LibRandomX()
    return _rx_instance


# ------------------------------------------------------------------ #
# Native batch miner (librxbatch.so)                                   #
# ------------------------------------------------------------------ #

_batch_lib: "ctypes.CDLL | None" = None
_batch_lib_lock = threading.Lock()


def _load_batch_lib() -> "ctypes.CDLL | None":
    """Load librxbatch.so if available. Returns None if not found."""
    paths = [
        os.path.join(os.path.dirname(__file__), "librxbatch.so"),
        os.path.join(os.path.dirname(__file__), "RandomX", "build", "librxbatch.so"),
    ]
    for p in paths:
        if os.path.exists(p):
            lib = ctypes.CDLL(p)
            # int64_t rx_batch_mine(vm, blob, blen, nonce_off,
            #   nonce_start, nonce_stride, batch_size, target,
            #   *out_nonce, *out_hash)
            lib.rx_batch_mine.restype  = ctypes.c_int64
            lib.rx_batch_mine.argtypes = [
                ctypes.c_void_p,   # vm
                ctypes.c_char_p,   # blob_template
                ctypes.c_int,      # blob_len
                ctypes.c_int,      # nonce_off
                ctypes.c_uint32,   # nonce_start
                ctypes.c_uint32,   # nonce_stride
                ctypes.c_int,      # batch_size
                ctypes.c_uint64,   # target
                ctypes.POINTER(ctypes.c_uint32),  # out_nonce
                ctypes.c_char_p,   # out_hash (32 bytes)
            ]
            logger.info(f"Loaded native batch miner from {p}")
            return lib
    return None


def get_batch_lib() -> "ctypes.CDLL | None":
    global _batch_lib
    if _batch_lib is None:
        with _batch_lib_lock:
            if _batch_lib is None:
                _batch_lib = _load_batch_lib()
    return _batch_lib


# ------------------------------------------------------------------ #
# Shared full dataset (fast mode)                                      #
# ------------------------------------------------------------------ #

_dataset_lock = threading.Lock()
_dataset_instance: "SharedDataset | None" = None
_dataset_failed: bool = False          # OOM — don't retry
_dataset_failed_seed: "bytes | None" = None  # only skip for this seed


class SharedDataset:
    """Full RandomX dataset (~2.4 GB) shared read-only across all threads.

    5–10x faster than per-thread light-mode caches. Built once per seed
    (Monero rotates seed every ~2048 blocks ≈ 2 days).
    """

    def __init__(self, seed: bytes, n_threads: int = 1):
        self._rx = get_rx()
        self.seed = seed
        lib = self._rx._lib

        flags = lib.randomx_get_flags() | RANDOMX_FLAG_FULL_MEM
        self.flags = flags

        cache = lib.randomx_alloc_cache(flags)
        if not cache:
            raise MemoryError("randomx_alloc_cache returned NULL")
        lib.randomx_init_cache(cache, seed, len(seed))
        self._cache = cache

        logger.info("Allocating full RandomX dataset (~2.4 GB)…")
        dataset = lib.randomx_alloc_dataset(flags)
        if not dataset:
            lib.randomx_release_cache(cache)
            self._cache = None
            raise MemoryError(
                "randomx_alloc_dataset returned NULL — not enough RAM for fast mode"
            )

        count = lib.randomx_dataset_item_count()
        n = max(1, n_threads)
        logger.info(
            f"Building dataset ({count:,} items) using {n} thread(s) — "
            "this takes ~30–90 s, then hashrate will jump…"
        )
        t0 = time.monotonic()

        chunk = count // n
        workers = []
        for i in range(n):
            start = ctypes.c_ulong(i * chunk)
            length = ctypes.c_ulong(
                (count - i * chunk) if (i == n - 1) else chunk
            )
            t = threading.Thread(
                target=lib.randomx_init_dataset,
                args=(dataset, cache, start, length),
                daemon=True,
            )
            t.start()
            workers.append(t)
        for t in workers:
            t.join()

        self._dataset = dataset
        elapsed = time.monotonic() - t0
        logger.info(f"Full dataset ready in {elapsed:.1f} s — fast mode active")

    def create_vm(self) -> int:
        return self._rx.create_vm(self.flags, self._cache, self._dataset)

    def close(self):
        lib = self._rx._lib
        if self._dataset:
            lib.randomx_release_dataset(self._dataset)
            self._dataset = None
        if self._cache:
            lib.randomx_release_cache(self._cache)
            self._cache = None


def get_shared_dataset(seed: bytes) -> "SharedDataset | None":
    """Return the shared dataset for *seed*, building it if needed.

    Thread-safe. All callers block until the dataset is ready.
    Returns None if the machine lacks RAM (graceful fallback to light mode).
    """
    global _dataset_instance, _dataset_failed, _dataset_failed_seed

    # Fast path: dataset ready for this seed
    if _dataset_instance is not None and _dataset_instance.seed == seed:
        return _dataset_instance

    # Fast path: known-failed for this seed
    if _dataset_failed and _dataset_failed_seed == seed:
        return None

    with _dataset_lock:
        # Re-check under lock
        if _dataset_instance is not None and _dataset_instance.seed == seed:
            return _dataset_instance
        if _dataset_failed and _dataset_failed_seed == seed:
            return None

        # Tear down stale dataset (seed rotated)
        if _dataset_instance is not None:
            logger.info("Seed changed — rebuilding full dataset…")
            _dataset_instance.close()
            _dataset_instance = None
            _dataset_failed = False
            _dataset_failed_seed = None

        try:
            _dataset_instance = SharedDataset(seed, num_dataset_threads)
            return _dataset_instance
        except MemoryError as exc:
            logger.warning(f"Fast mode unavailable ({exc}). Falling back to light mode.")
            _dataset_failed = True
            _dataset_failed_seed = seed
            return None


# ------------------------------------------------------------------ #
# Per-thread VM                                                        #
# ------------------------------------------------------------------ #

class RandomXVM:
    """Per-thread RandomX VM.

    On first `ensure_seed` call it attempts fast mode (shared full dataset).
    If RAM is insufficient it automatically falls back to light mode.
    """

    def __init__(self):
        self._rx = get_rx()
        self._cache: int | None = None
        self._vm: int | None = None
        self._flags: int = 0
        self._current_seed: bytes | None = None
        self._fast_mode: bool = False
        # Pre-allocated 32-byte output buffer — reused to avoid GC pressure
        self._out = ctypes.create_string_buffer(32)

    def ensure_seed(self, seed_hash_hex: str):
        seed = bytes.fromhex(seed_hash_hex) if seed_hash_hex else b"\x00" * 32
        if seed == self._current_seed:
            return

        # Tear down old VM / cache (dataset is shared — don't close it)
        if self._vm:
            self._rx.destroy_vm(self._vm)
            self._vm = None
        if self._cache:
            self._rx.release_cache(self._cache)
            self._cache = None

        # Try fast mode first
        dataset = get_shared_dataset(seed)
        if dataset:
            self._flags = dataset.flags
            self._vm = dataset.create_vm()
            self._fast_mode = True
        else:
            # Light mode: per-thread cache
            self._flags = self._rx.get_flags(light_mode=True)
            logger.debug(f"Initialising RandomX cache for seed {seed_hash_hex[:16]}…")
            cache = self._rx.alloc_cache(self._flags)
            self._rx.init_cache(cache, seed)
            self._cache = cache
            self._vm = self._rx.create_vm(self._flags, cache)
            self._fast_mode = False

        self._current_seed = seed
        logger.debug(f"RandomX VM ready ({'fast' if self._fast_mode else 'light'} mode)")

    # ---------------------------------------------------------------- #
    # Hashing                                                           #
    # ---------------------------------------------------------------- #

    def hash(self, data: bytes) -> bytes:
        self._rx.calculate_hash(self._vm, data, self._out)
        return bytes(self._out)

    def hash_first(self, data: bytes) -> None:
        self._rx.hash_first(self._vm, data)

    def hash_next(self, data: bytes) -> bytes:
        self._rx.hash_next(self._vm, data, self._out)
        return bytes(self._out)

    def hash_last(self) -> bytes:
        self._rx.hash_last(self._vm, self._out)
        return bytes(self._out)

    def close(self):
        if self._vm:
            self._rx.destroy_vm(self._vm)
            self._vm = None
        if self._cache:
            self._rx.release_cache(self._cache)
            self._cache = None
