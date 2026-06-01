"""
Python ctypes wrapper around librandomx.so

Loads the shared library built from https://github.com/tevador/RandomX
and exposes the minimal surface needed for mining:
  - create / destroy VM
  - calculate hash
"""

import ctypes
import ctypes.util
import os
import logging
import threading

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


def _load_library() -> ctypes.CDLL:
    """Locate and load librandomx, searching common paths."""
    search_paths = [
        # Locally built library (placed by build script)
        os.path.join(os.path.dirname(__file__), "RandomX", "build", "librandomx.so"),
        os.path.join(os.path.dirname(__file__), "librandomx.so"),
        # System paths
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
        "librandomx.so not found. "
        "Run: cd miner/RandomX && mkdir build && cd build && cmake .. && make -j$(nproc)"
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

    # ------------------------------------------------------------------ #
    # Public helpers                                                        #
    # ------------------------------------------------------------------ #

    def get_flags(self, light_mode: bool = True) -> int:
        base = self._lib.randomx_get_flags()
        if light_mode:
            base &= ~RANDOMX_FLAG_FULL_MEM
        return base

    def alloc_cache(self, flags: int) -> ctypes.c_void_p:
        ptr = self._lib.randomx_alloc_cache(flags)
        if not ptr:
            raise MemoryError("randomx_alloc_cache returned NULL")
        return ptr

    def init_cache(self, cache: ctypes.c_void_p, key: bytes):
        self._lib.randomx_init_cache(cache, key, len(key))

    def release_cache(self, cache: ctypes.c_void_p):
        self._lib.randomx_release_cache(cache)

    def create_vm(self, flags: int, cache: ctypes.c_void_p) -> ctypes.c_void_p:
        vm = self._lib.randomx_create_vm(flags, cache, None)
        if not vm:
            raise MemoryError("randomx_create_vm returned NULL")
        return vm

    def destroy_vm(self, vm: ctypes.c_void_p):
        self._lib.randomx_destroy_vm(vm)

    def calculate_hash(self, vm: ctypes.c_void_p, data: bytes, out: ctypes.Array) -> None:
        self._lib.randomx_calculate_hash(vm, data, len(data), out)

    def hash_first(self, vm: ctypes.c_void_p, data: bytes) -> None:
        self._lib.randomx_calculate_hash_first(vm, data, len(data))

    def hash_next(self, vm: ctypes.c_void_p, data: bytes, out: ctypes.Array) -> None:
        self._lib.randomx_calculate_hash_next(vm, data, len(data), out)

    def hash_last(self, vm: ctypes.c_void_p, out: ctypes.Array) -> None:
        self._lib.randomx_calculate_hash_last(vm, out)


# Module-level singleton
_rx_lock = threading.Lock()
_rx_instance: "_LibRandomX | None" = None


def get_rx() -> _LibRandomX:
    global _rx_instance
    if _rx_instance is None:
        with _rx_lock:
            if _rx_instance is None:
                _rx_instance = _LibRandomX()
    return _rx_instance


class RandomXVM:
    """Per-thread VM that caches the dataset for a given seed hash."""

    def __init__(self, light_mode: bool = True):
        self._rx = get_rx()
        self._flags = self._rx.get_flags(light_mode)
        self._cache: ctypes.c_void_p | None = None
        self._vm: ctypes.c_void_p | None = None
        self._current_seed: bytes | None = None
        # Pre-allocated output buffer — reused every hash to avoid GC pressure
        self._out = ctypes.create_string_buffer(32)

    def ensure_seed(self, seed_hash_hex: str):
        seed = bytes.fromhex(seed_hash_hex) if seed_hash_hex else b"\x00" * 32
        if seed == self._current_seed:
            return

        # Tear down old VM / cache
        if self._vm:
            self._rx.destroy_vm(self._vm)
            self._vm = None
        if self._cache:
            self._rx.release_cache(self._cache)
            self._cache = None

        logger.debug(f"Initialising RandomX cache for seed {seed_hash_hex[:16]}…")
        cache = self._rx.alloc_cache(self._flags)
        self._rx.init_cache(cache, seed)
        self._cache = cache
        self._vm = self._rx.create_vm(self._flags, cache)
        self._current_seed = seed
        logger.debug("RandomX VM ready")

    def hash(self, data: bytes) -> bytes:
        """Single blocking hash — returns 32-byte result."""
        self._rx.calculate_hash(self._vm, data, self._out)
        return bytes(self._out)

    # ------------------------------------------------------------------ #
    # Pipelined hash API — ~25-30% faster throughput by overlapping       #
    # AES rounds of the next input with finalisation of the previous one. #
    # Usage: hash_first(d0) → loop: h=hash_next(d_n) → hash_last()       #
    # ------------------------------------------------------------------ #

    def hash_first(self, data: bytes) -> None:
        """Submit the very first input into the pipeline."""
        self._rx.hash_first(self._vm, data)

    def hash_next(self, data: bytes) -> bytes:
        """Finish previous hash, start next — returns previous result."""
        self._rx.hash_next(self._vm, data, self._out)
        return bytes(self._out)

    def hash_last(self) -> bytes:
        """Flush the last pending hash out of the pipeline."""
        self._rx.hash_last(self._vm, self._out)
        return bytes(self._out)

    def close(self):
        if self._vm:
            self._rx.destroy_vm(self._vm)
            self._vm = None
        if self._cache:
            self._rx.release_cache(self._cache)
            self._cache = None
