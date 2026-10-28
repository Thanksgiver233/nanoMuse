"""Privilege separation, Linux prototype: the gatekeeper holds the keys.

One process, the gatekeeper, holds three things the agent process must never
touch: the Sentinel rules (as a settings snapshot loaded once at start), the
persistent grants file, and the audit signing key. The agent side
(:class:`GatekeeperSentinel`) computes the policy's advice exactly as the
in-process Sentinel would, then asks the gatekeeper over a Unix socket for
the final decision; the gatekeeper re-evaluates with its own copy of the
rules, checks its own grants, signs the audit entry, and answers. The key
never crosses the socket, in either direction.

The key reaches the gatekeeper one of two ways, and no other: it is passed on
a file descriptor at spawn (the launcher's job), or it is read from the OS
keychain. There is deliberately no file fallback: when neither is available
the gatekeeper refuses to start, and the agent side refuses to run a call
(fail closed), rather than quietly running without a gate.

What this prototype is not: the gatekeeper is not wired into the running
agent (the in-process Sentinel stays the default), the socket trusts the
same-UID peer credential (a deployment that separates UIDs would check more),
and the rules snapshot is loaded once, so editing the rules file afterwards
changes nothing until the gatekeeper restarts. Those limits are stated here
because the prototype's tests assert the boundary as built, not as wished.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import re
import socket
import struct
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from nanomuse.config import SentinelSettings
from nanomuse.logger import logger
from nanomuse.sentinel.audit import AuditLog, HmacSigner, _canonical
from nanomuse.sentinel.grants import GrantStore, grant_key
from nanomuse.sentinel.policy import Decision, Policy
from nanomuse.tools.base import CallAssessment

_KEYRING_SERVICE = "nanomuse-gatekeeper"
_KEYRING_ACCOUNT = "audit-signing"
_SOCKET_TIMEOUT_S = 30.0

_SECRET_KEYS = re.compile(
    r"(pass(word|wd|phrase)?|secret|token|api[_-]?key|auth|credential|cookie|otp|code|pin)",
    re.IGNORECASE,
)


class GatekeeperUnavailable(RuntimeError):
    """The gatekeeper cannot be reached, or cannot start: the caller must refuse
    the call and say why, never run it without a decision."""


class GatekeeperError(RuntimeError):
    pass


# ------------------------------------------------------------------ the key
def _read_key_from_fd(fd: int) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while total < 32:
        chunk = os.read(fd, 32 - total)
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
    key = b"".join(chunks)
    if len(key) != 32:
        raise GatekeeperUnavailable(f"the key passed at spawn must be 32 bytes, got {len(key)}")
    return key


def resolve_gatekeeper_key(key_fd: int | None) -> bytes:
    """The gatekeeper's signing key: from the spawn file descriptor, else from
    the OS keychain. Nothing else: no file, no environment, no fallback."""
    if key_fd is not None:
        try:
            return _read_key_from_fd(key_fd)
        finally:
            os.close(key_fd)
    try:
        import keyring  # type: ignore[import-not-found]
    except ImportError:
        keyring = None
    if keyring is not None:
        try:
            stored = keyring.get_password(_KEYRING_SERVICE, _KEYRING_ACCOUNT)
        except Exception:  # noqa: BLE001 - no keychain backend: unavailable, not fatal
            stored = None
        if isinstance(stored, str) and stored:
            try:
                key = bytes.fromhex(stored.strip())
            except ValueError:
                key = b""
            if len(key) == 32:
                return key
    raise GatekeeperUnavailable(
        "no gatekeeper key is available: pass one on a file descriptor at spawn, "
        "or store one in the OS keychain; refusing to fall back to a file"
    )


# ------------------------------------------------------------------ the server
def _shape(value: Any, depth: int = 0) -> Any:
    """What of a call's arguments the audit line may say: shapes, never values."""
    if isinstance(value, dict):
        if depth >= 2:
            return {"keys": len(value)}
        return {
            str(k): "***" if _SECRET_KEYS.search(str(k)) else _shape(v, depth + 1)
            for k, v in list(value.items())[:12]
        }
    if isinstance(value, str):
        return f"str[{len(value)}]"
    if isinstance(value, (list, tuple)):
        return f"list[{len(value)}]"
    return value


class GatekeeperServer:
    """The decision side. One connection at a time, in arrival order: the audit
    chain is written in the order decisions are made."""

    def __init__(
        self,
        socket_path: Path,
        settings: SentinelSettings,
        grants_path: Path | None,
        audit_path: Path,
        key: bytes,
    ):
        self.socket_path = Path(socket_path)
        self.policy = Policy(settings)
        self.grants = GrantStore(grants_path)
        self.audit = AuditLog(audit_path, signer=HmacSigner(key), key_id="gatekeeper")
        self._stop = False

    # ------------------------------------------------------------------ socket
    def _peer_uid(self, conn: socket.socket) -> int:
        try:
            raw = conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
        except (AttributeError, OSError) as exc:
            raise GatekeeperError("SO_PEERCRED is unavailable on this platform") from exc
        _pid, uid, _gid = struct.unpack("3i", raw)
        return uid

    def serve_forever(self) -> None:
        self.socket_path.parent.mkdir(parents=True, exist_ok=True)
        if self.socket_path.exists():
            self.socket_path.unlink()
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            server.bind(str(self.socket_path))
            os.chmod(self.socket_path, 0o600)
            server.listen(8)
            server.settimeout(0.5)
            print("READY", flush=True)
            while not self._stop:
                try:
                    conn, _ = server.accept()
                except TimeoutError:
                    continue
                try:
                    self._serve_connection(conn)
                except Exception as exc:  # noqa: BLE001 - one bad peer ends one connection
                    logger.warning("gatekeeper: connection failed: {}", exc)
                finally:
                    conn.close()
        finally:
            server.close()
            if self.socket_path.exists():
                self.socket_path.unlink()

    def _serve_connection(self, conn: socket.socket) -> None:
        if self._peer_uid(conn) != os.getuid():
            logger.warning("gatekeeper: refusing a peer from another user")
            return
        conn.settimeout(_SOCKET_TIMEOUT_S)
        buf = b""
        while not self._stop:
            try:
                chunk = conn.recv(65536)
            except TimeoutError:
                return
            if not chunk:
                return
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                if line.strip():
                    response = self._handle(line)
                    conn.sendall(json.dumps(response, ensure_ascii=False).encode("utf-8") + b"\n")

    # ------------------------------------------------------------------ requests
    def _handle(self, line: bytes) -> dict[str, Any]:
        try:
            request = json.loads(line.decode("utf-8"))
        except ValueError:
            return {"id": None, "error": "request is not JSON"}
        if not isinstance(request, dict):
            return {"id": None, "error": "request is not an object"}
        op = request.get("op")
        req_id = request.get("id")
        try:
            if op == "ping":
                return {"id": req_id, "ok": True}
            if op == "decide":
                return self._decide(req_id, request)
            if op == "verify":
                return self._verify(req_id, request)
            return {"id": req_id, "error": f"unknown op {str(op)[:40]!r}"}
        except Exception as exc:  # noqa: BLE001 - the peer gets a reason, never a traceback
            return {"id": req_id, "error": f"{type(exc).__name__}: {str(exc)[:200]}"}

    def _decide(self, req_id: Any, request: dict[str, Any]) -> dict[str, Any]:
        tool = str(request.get("tool") or "")
        args = request.get("args") or {}
        if not isinstance(args, dict):
            raise GatekeeperError("args must be an object")
        assessment = CallAssessment.model_validate(request.get("assessment") or {})
        tainted = bool(request.get("tainted", False))
        conversation = request.get("conversation")
        result = self.policy.evaluate(tool, args, assessment, tainted=tainted)
        decision, reasons = result.decision, list(result.reasons)
        scope: str | None = None
        if decision == Decision.ASK:
            key = grant_key(tool, assessment.target or assessment.egress_target)
            grant = self.grants.match(key, conversation)
            if grant is not None:
                decision = Decision.ALLOW
                scope = grant.scope
                reasons.append(f"covered by your '{grant.scope}' permission for {grant.key}")
        advice = request.get("advice") or {}
        entry = self.audit.record(
            "tool_call",
            tool=tool,
            channel="gatekeeper",
            args=_shape(args),
            summary=assessment.summary,
            risk=assessment.risk.value,
            decision=decision.value,
            advice_decision=str(advice.get("decision") or ""),
            reasons=reasons,
            tainted=tainted,
            egress_target=assessment.egress_target,
            grant_scope=scope,
        )
        return {
            "id": req_id,
            "decision": decision.value,
            "reasons": reasons,
            "tainted_ask": result.tainted_ask,
            "grant_scope": scope,
            "audit_seq": entry["seq"],
        }

    def _verify(self, req_id: Any, request: dict[str, Any]) -> dict[str, Any]:
        entry = request.get("entry")
        if not isinstance(entry, dict):
            raise GatekeeperError("entry must be an object")
        tag = entry.get("entry_hash")
        signer = self.audit._signer
        valid = (
            isinstance(tag, str) and signer is not None and signer.verify(_canonical(entry), tag)
        )
        return {"id": req_id, "valid": valid}


# ------------------------------------------------------------------ the client
@dataclass(frozen=True)
class GatekeeperDecision:
    decision: Decision
    reasons: list[str] = field(default_factory=list)
    tainted_ask: bool = False
    grant_scope: str | None = None
    audit_seq: int | None = None


class GatekeeperClient:
    """The agent side: one request per connection, so a dead gatekeeper is
    noticed on the next call instead of carried as a stale socket."""

    def __init__(self, socket_path: Path | str, timeout: float = 5.0):
        self.socket_path = str(socket_path)
        self.timeout = timeout

    def _call(self, payload: dict[str, Any]) -> dict[str, Any]:
        line = json.dumps(payload, ensure_ascii=False).encode("utf-8") + b"\n"
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            sock.settimeout(self.timeout)
            sock.connect(self.socket_path)
            sock.sendall(line)
            buf = b""
            while b"\n" not in buf:
                chunk = sock.recv(65536)
                if not chunk:
                    raise GatekeeperUnavailable("the gatekeeper closed the connection")
                buf += chunk
        except GatekeeperUnavailable:
            raise
        except (TimeoutError, OSError) as exc:
            raise GatekeeperUnavailable(f"the gatekeeper cannot be reached: {exc}") from exc
        finally:
            with contextlib.suppress(Exception):
                sock.close()
        try:
            response = json.loads(buf.decode("utf-8"))
        except ValueError as exc:
            raise GatekeeperUnavailable("the gatekeeper answered with bad JSON") from exc
        if isinstance(response, dict) and response.get("error"):
            raise GatekeeperUnavailable(f"the gatekeeper refused the request: {response['error']}")
        return response

    def ping(self) -> bool:
        try:
            return bool(self._call({"id": "ping", "op": "ping"}).get("ok"))
        except GatekeeperUnavailable:
            return False

    def decide(
        self,
        tool: str,
        args: dict[str, Any],
        assessment: CallAssessment,
        tainted: bool = False,
        conversation: str | None = None,
        advice: dict[str, Any] | None = None,
    ) -> GatekeeperDecision:
        response = self._call(
            {
                "id": f"decide-{time.monotonic_ns()}",
                "op": "decide",
                "tool": tool,
                "args": args,
                "assessment": assessment.model_dump(mode="json"),
                "tainted": tainted,
                "conversation": conversation,
                "advice": advice or {},
            }
        )
        return GatekeeperDecision(
            decision=Decision(str(response["decision"])),
            reasons=list(response.get("reasons") or []),
            tainted_ask=bool(response.get("tainted_ask")),
            grant_scope=response.get("grant_scope"),
            audit_seq=response.get("audit_seq"),
        )

    def verify_entry(self, entry: dict[str, Any]) -> bool:
        response = self._call({"id": "verify", "op": "verify", "entry": entry})
        return bool(response.get("valid"))


class GatekeeperSentinel:
    """A Sentinel-shaped front end over the gatekeeper: the policy runs here
    for the advice, the decision comes back from the other process."""

    def __init__(self, settings: SentinelSettings, client: GatekeeperClient):
        self.settings = settings
        self.policy = Policy(settings)
        self.client = client

    def decide(
        self,
        tool: str,
        args: dict[str, Any],
        assessment: CallAssessment,
        tainted: bool = False,
        conversation: str | None = None,
    ) -> GatekeeperDecision:
        advice = self.policy.evaluate(tool, args, assessment, tainted=tainted)
        return self.client.decide(
            tool,
            args,
            assessment,
            tainted=tainted,
            conversation=conversation,
            advice={"decision": advice.decision.value, "reasons": advice.reasons},
        )


# ------------------------------------------------------------------ supervisor
class GatekeeperSupervisor:
    """Spawn the gatekeeper, hand it the key on a file descriptor, watch it die.

    The supervisor holds the key material only to pass it at spawn; the agent
    process that uses the client never sees it. ``key_material`` of None means
    the child must find the key in the OS keychain, or refuse to start."""

    def __init__(
        self,
        socket_path: Path,
        settings: SentinelSettings,
        grants_path: Path | None,
        audit_path: Path,
        key_material: bytes | None,
        python: str = sys.executable,
    ):
        self.socket_path = Path(socket_path)
        self.settings = settings
        self.grants_path = grants_path
        self.audit_path = Path(audit_path)
        self.key_material = key_material
        self._python = python
        self._proc: asyncio.subprocess.Process | None = None
        self._stderr: asyncio.Task[None] | None = None
        self.stderr_lines: list[str] = []
        self.started = False

    async def start(self) -> None:
        if self.started:
            return
        workdir = self.socket_path.parent
        workdir.mkdir(parents=True, exist_ok=True)
        settings_path = workdir / "gatekeeper-settings.json"
        settings_path.write_text(
            json.dumps(self.settings.model_dump(mode="json"), ensure_ascii=False), "utf-8"
        )
        args = [
            self._python,
            "-m",
            "nanomuse.sentinel.gatekeeper",
            "--socket",
            str(self.socket_path),
            "--settings",
            str(settings_path),
            "--audit",
            str(self.audit_path),
        ]
        if self.grants_path is not None:
            args += ["--grants", str(self.grants_path)]
        pass_fds: tuple[int, ...] = ()
        read_fd: int | None = None
        write_fd: int | None = None
        if self.key_material is not None:
            read_fd, write_fd = os.pipe()
            args += ["--key-fd", str(read_fd)]
            pass_fds = (read_fd,)
        self._proc = await asyncio.create_subprocess_exec(
            *args,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            pass_fds=pass_fds,
            close_fds=True,
        )
        try:
            if read_fd is not None and write_fd is not None:
                os.close(read_fd)
                os.write(write_fd, self.key_material or b"")
                os.close(write_fd)
            assert self._proc.stdout is not None
            line = await asyncio.wait_for(self._proc.stdout.readline(), timeout=15)
            if line.strip() != b"READY":
                err = b""
                if self._proc.stderr is not None:
                    with contextlib.suppress(asyncio.TimeoutError):
                        err = await asyncio.wait_for(self._proc.stderr.read(), timeout=2)
                code = await self._proc.wait()
                raise GatekeeperUnavailable(
                    f"the gatekeeper did not start (exit {code}): {err.decode(errors='replace')[-500:]}"
                )
        except Exception:
            await self.stop()
            raise
        assert self._proc.stderr is not None
        self._stderr = asyncio.create_task(self._drain_stderr())
        self.started = True

    async def _drain_stderr(self) -> None:
        assert self._proc is not None and self._proc.stderr is not None
        try:
            async for raw in self._proc.stderr:
                self.stderr_lines.append(raw.decode("utf-8", errors="replace").rstrip())
        except asyncio.CancelledError:
            return

    async def stop(self) -> None:
        self.started = False
        proc, self._proc = self._proc, None
        if proc is not None and proc.returncode is None:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=10)
            except TimeoutError:
                proc.kill()
                await proc.wait()
        if self._stderr is not None:
            self._stderr.cancel()
            self._stderr = None
        if self.socket_path.exists():
            with contextlib.suppress(OSError):
                self.socket_path.unlink()

    async def __aenter__(self) -> GatekeeperSupervisor:
        await self.start()
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.stop()


# ------------------------------------------------------------------ entry point
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="The Sentinel gatekeeper process.")
    parser.add_argument("--socket", type=Path, required=True)
    parser.add_argument("--settings", type=Path, required=True)
    parser.add_argument("--grants", type=Path, default=None)
    parser.add_argument("--audit", type=Path, required=True)
    parser.add_argument("--key-fd", type=int, default=None)
    args = parser.parse_args(argv)
    try:
        settings = SentinelSettings.model_validate(json.loads(args.settings.read_text("utf-8")))
        key = resolve_gatekeeper_key(args.key_fd)
    except (GatekeeperUnavailable, ValueError, OSError) as exc:
        print(f"gatekeeper: {exc}", file=sys.stderr)
        return 1
    server = GatekeeperServer(args.socket, settings, args.grants, args.audit, key)
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "GatekeeperClient",
    "GatekeeperDecision",
    "GatekeeperError",
    "GatekeeperSentinel",
    "GatekeeperServer",
    "GatekeeperSupervisor",
    "GatekeeperUnavailable",
    "resolve_gatekeeper_key",
]
