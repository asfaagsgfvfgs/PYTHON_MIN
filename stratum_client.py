"""
Stratum v1 protocol client for Monero (RandomX) mining pools.
Handles login, job subscription, share submission, and reconnection.
"""

import json
import socket
import ssl
import threading
import time
import logging
from typing import Callable, Optional

logger = logging.getLogger(__name__)


class StratumJob:
    def __init__(self, job_id: str, blob: str, target: str, seed_hash: str, height: int):
        self.job_id = job_id
        self.blob = bytes.fromhex(blob)
        self.target = target
        self.seed_hash = seed_hash
        self.height = height
        self.target_int = self._parse_target(target)

    def _parse_target(self, target: str) -> int:
        raw = bytes.fromhex(target)
        if len(raw) == 4:
            # 4-byte compact target (LE uint32) → expand to 64-bit for comparison
            # target_64 = compact << 32  (compact occupies the upper 32 bits)
            compact = int.from_bytes(raw, "little")
            if compact == 0:
                return 2**64 - 1
            return compact << 32
        if len(raw) == 8:
            return int.from_bytes(raw, "little")
        # Fallback: treat as LE, pad/truncate to 8 bytes
        return int.from_bytes(raw.ljust(8, b"\xff")[:8], "little")

    def meets_target(self, hash_bytes: bytes) -> bool:
        # RandomX difficulty check: last 8 bytes of hash as LE uint64 vs target_64
        # This matches XMRig: hash[24:32] < target
        result = int.from_bytes(hash_bytes[24:32], "little")
        return result < self.target_int


class StratumClient:
    RECONNECT_DELAY = 5  # seconds between reconnect attempts

    def __init__(
        self,
        host: str,
        port: int,
        wallet: str,
        worker: str,
        password: str = "x",
        use_tls: bool = False,
        on_job: Optional[Callable[[StratumJob], None]] = None,
    ):
        self.host = host
        self.port = port
        self.wallet = wallet
        self.worker = worker
        self.password = password
        self.use_tls = use_tls
        self.on_job = on_job

        self._sock: Optional[socket.socket] = None
        self._send_lock = threading.Lock()
        self._request_id = 1
        self._running = False
        self._receive_thread: Optional[threading.Thread] = None
        self._session_id: Optional[str] = None

        # Stats
        self.accepted = 0
        self.rejected = 0
        self.current_job: Optional[StratumJob] = None

    # ------------------------------------------------------------------ #
    # Connection lifecycle                                                  #
    # ------------------------------------------------------------------ #

    def connect(self) -> bool:
        try:
            raw = socket.create_connection((self.host, self.port), timeout=30)
            if self.use_tls:
                ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
                ctx.check_hostname = False
                ctx.verify_mode = ssl.CERT_NONE
                ctx.minimum_version = ssl.TLSVersion.TLSv1_2
                try:
                    self._sock = ctx.wrap_socket(raw, server_hostname=self.host)
                except ssl.SSLError as tls_err:
                    logger.warning(f"TLS failed ({tls_err}), retrying without TLS")
                    raw.close()
                    raw = socket.create_connection((self.host, self.port), timeout=30)
                    self._sock = raw
            else:
                self._sock = raw
            logger.info(f"Connected to {self.host}:{self.port}")
            return True
        except OSError as exc:
            logger.error(f"Connection failed: {exc}")
            return False

    def start(self):
        self._running = True
        self._connect_and_login()

    def stop(self):
        self._running = False
        if self._sock:
            try:
                self._sock.close()
            except OSError:
                pass

    def _connect_and_login(self):
        while self._running:
            if self.connect():
                if self._login():
                    self._receive_thread = threading.Thread(
                        target=self._receive_loop, daemon=True
                    )
                    self._receive_thread.start()
                    return
            logger.warning(f"Retrying in {self.RECONNECT_DELAY}s…")
            time.sleep(self.RECONNECT_DELAY)

    def _reconnect(self):
        logger.warning("Disconnected — attempting reconnect")
        if self._sock:
            try:
                self._sock.close()
            except OSError:
                pass
        time.sleep(self.RECONNECT_DELAY)
        self._connect_and_login()

    # ------------------------------------------------------------------ #
    # Stratum protocol                                                      #
    # ------------------------------------------------------------------ #

    def _next_id(self) -> int:
        rid = self._request_id
        self._request_id += 1
        return rid

    def _send(self, msg: dict):
        data = json.dumps(msg) + "\n"
        with self._send_lock:
            try:
                self._sock.sendall(data.encode())
            except OSError as exc:
                logger.error(f"Send error: {exc}")
                if self._running:
                    threading.Thread(target=self._reconnect, daemon=True).start()

    def _login(self) -> bool:
        login = {
            "id": self._next_id(),
            "method": "login",
            "params": {
                "login": self.wallet,
                "pass": self.password,
                "agent": "py-randomx-miner/1.0",
                "rigid": self.worker,
            },
        }
        self._send(login)

        # Wait synchronously for the login response
        buf = b""
        self._sock.settimeout(15)
        try:
            while b"\n" not in buf:
                chunk = self._sock.recv(4096)
                if not chunk:
                    return False
                buf += chunk
        except socket.timeout:
            logger.error("Login timeout")
            return False
        finally:
            self._sock.settimeout(None)

        line = buf.split(b"\n")[0]
        msg = json.loads(line)
        result = msg.get("result")
        if not result:
            logger.error(f"Login failed: {msg.get('error')}")
            return False

        self._session_id = result.get("id")
        job_data = result.get("job")
        if job_data:
            self._handle_job(job_data)

        logger.info(f"Logged in. Session: {self._session_id}")
        return True

    def _receive_loop(self):
        buf = b""
        while self._running:
            try:
                chunk = self._sock.recv(4096)
                if not chunk:
                    break
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    try:
                        self._handle_message(json.loads(line))
                    except Exception as exc:
                        logger.warning(f"Message handling error: {exc}")
            except (OSError, json.JSONDecodeError) as exc:
                logger.error(f"Receive error: {exc}")
                break

        if self._running:
            threading.Thread(target=self._reconnect, daemon=True).start()

    def _handle_message(self, msg: dict):
        method = msg.get("method")
        if method == "job":
            self._handle_job(msg["params"])
        elif "result" in msg:
            result = msg["result"]
            error = msg.get("error")
            if error:
                logger.warning(f"Share rejected: {error}")
                self.rejected += 1
            elif result is True or (isinstance(result, dict) and result.get("status") == "OK"):
                logger.info("Share accepted ✓")
                self.accepted += 1

    def _handle_job(self, job_data: dict):
        job = StratumJob(
            job_id=job_data["job_id"],
            blob=job_data["blob"],
            target=job_data["target"],
            seed_hash=job_data.get("seed_hash", ""),
            height=int(job_data.get("height", 0)),
        )
        self.current_job = job
        logger.info(
            f"New job #{job.job_id} height={job.height} seed={job.seed_hash[:16]}…"
        )
        if self.on_job:
            self.on_job(job)

    def submit_share(self, job_id: str, nonce_hex: str, result_hex: str):
        share = {
            "id": self._next_id(),
            "method": "submit",
            "params": {
                "id": self._session_id,
                "job_id": job_id,
                "nonce": nonce_hex,
                "result": result_hex,
            },
        }
        self._send(share)
        logger.debug(f"Submitted share nonce={nonce_hex}")
