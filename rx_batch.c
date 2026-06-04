/*
 * rx_batch.c — native batch mining loop for py-randomx-miner
 *
 * Called from Python via ctypes. Runs entirely in C with the GIL released,
 * giving true multi-core parallelism and eliminating per-hash Python overhead.
 *
 * Compile (from the miner/ directory):
 *   gcc -O3 -march=native -shared -fPIC \
 *       -I RandomX/src rx_batch.c \
 *       -L RandomX/build -Wl,-rpath,'$ORIGIN/RandomX/build' -lrandomx \
 *       -o librxbatch.so
 */

#include <stdint.h>
#include <string.h>

/* Pull in only the three pipeline symbols — avoids needing the full header. */
typedef void randomx_vm;
void randomx_calculate_hash_first(randomx_vm *machine,
                                   const void *input, size_t inputSize);
void randomx_calculate_hash_next (randomx_vm *machine,
                                   const void *nextInput, size_t nextInputSize,
                                   void *output);
void randomx_calculate_hash_last (randomx_vm *machine, void *output);

/*
 * rx_batch_mine()
 *
 * Mines `batch_size` nonces starting at `nonce_start` with stride
 * `nonce_stride`, writing each as 4 little-endian bytes into
 * blob_template[nonce_off].
 *
 * A hash passes when bytes [24..31] interpreted as a little-endian uint64
 * are strictly less than `target`.
 *
 * Returns: number of hashes actually computed.
 * Outputs:
 *   *out_nonce  — winning nonce, or 0xFFFFFFFF if none found in this batch.
 *   out_hash    — 32-byte hash of the winning nonce (valid when nonce != 0xFFFFFFFF).
 */
int64_t rx_batch_mine(
    randomx_vm  *vm,
    const uint8_t *blob_template,
    int            blob_len,
    int            nonce_off,
    uint32_t       nonce_start,
    uint32_t       nonce_stride,
    int            batch_size,
    uint64_t       target,
    uint32_t      *out_nonce,
    uint8_t       *out_hash        /* caller provides 32 bytes */
)
{
    uint8_t  work[256];
    uint8_t  cur_hash[32];
    int      blen = (blob_len <= 256) ? blob_len : 256;

    memcpy(work, blob_template, blen);

    /* Write first nonce and seed the pipeline. */
    uint32_t cur  = nonce_start;
    work[nonce_off + 0] = (uint8_t)(cur);
    work[nonce_off + 1] = (uint8_t)(cur >>  8);
    work[nonce_off + 2] = (uint8_t)(cur >> 16);
    work[nonce_off + 3] = (uint8_t)(cur >> 24);
    randomx_calculate_hash_first(vm, work, blen);

    uint32_t prev = cur;
    cur = cur + nonce_stride;          /* uint32 wraps naturally */

    *out_nonce = 0xFFFFFFFFu;

    /* ------------------------------------------------------------------ *
     * Main loop: hash_next(cur) returns the hash of `prev`.              *
     * ------------------------------------------------------------------ */
    for (int i = 1; i < batch_size; i++) {
        uint64_t val;

        work[nonce_off + 0] = (uint8_t)(cur);
        work[nonce_off + 1] = (uint8_t)(cur >>  8);
        work[nonce_off + 2] = (uint8_t)(cur >> 16);
        work[nonce_off + 3] = (uint8_t)(cur >> 24);
        randomx_calculate_hash_next(vm, work, blen, cur_hash);

        memcpy(&val, cur_hash + 24, 8);
        if (val < target) {
            *out_nonce = prev;
            memcpy(out_hash, cur_hash, 32);
            /* Drain pipeline so the VM is ready for the next batch. */
            randomx_calculate_hash_last(vm, cur_hash);
            return (int64_t)(i + 1);
        }

        prev = cur;
        cur  = cur + nonce_stride;
    }

    /* Drain the final pending hash. */
    {
        uint64_t val;
        randomx_calculate_hash_last(vm, cur_hash);
        memcpy(&val, cur_hash + 24, 8);
        if (val < target) {
            *out_nonce = prev;
            memcpy(out_hash, cur_hash, 32);
        }
    }

    return (int64_t)batch_size;
}
