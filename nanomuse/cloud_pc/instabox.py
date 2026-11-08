"""The InstaBox provider: one REST API for the machine and the work in it.

``https://api.instabox.sh`` with the key as a bearer header. InstaBox
machines are Firecracker microVMs with a persistent disk; on the free
plan there is exactly one size (4 vCPU, 8 GiB) and one machine at a time,
and an idle machine suspends itself after 30 minutes with its memory
kept. Changes (create, delete) answer ``202`` with an operation to
follow, and a delete also needs the machine's own id echoed in an
``X-Confirm-Delete`` header, which this client always sends.

One honest limit, named rather than smoothed over: sizes are the plan's,
so the create call sends no size at all and records what the machine
reports.

The desktop (A3) follows the provider's own SDK rather than its VNC
stream: every graphical verb is an exec inside the machine (``xdotool``
for input, ``scrot`` for the picture, ``DISPLAY=:1`` throughout) plus a
file download for the screenshot, over the same channels as the rest of
this client. The lifecycle (install, start, status) is the one call that
is a desktop endpoint of its own.
"""

from __future__ import annotations

import asyncio
import shlex
import uuid
from dataclasses import dataclass
from typing import Any

import httpx

from nanomuse.cloud_pc.daytona import CloudError
from nanomuse.cloud_pc.provider import MachineInfo

API_BASE = "https://api.instabox.sh"
DESKTOP_DISPLAY = ":1"

# Model-facing key names to xdotool keysyms, mirroring the table the
# local hands use (nanomuse/computer/hands.py) so a key is spelled the
# same way on a local screen and on this one. Names not in the table
# pass through as keysyms of their own (a, F5, Return and friends).
_XDO_KEYS = {
    "ctrl": "ctrl",
    "control": "ctrl",
    "alt": "alt",
    "option": "alt",
    "shift": "shift",
    "cmd": "super",
    "command": "super",
    "win": "super",
    "super": "super",
    "meta": "super",
    "enter": "Return",
    "return": "Return",
    "esc": "Escape",
    "escape": "Escape",
    "backspace": "BackSpace",
    "delete": "Delete",
    "del": "Delete",
    "tab": "Tab",
    "space": "space",
    "up": "Up",
    "down": "Down",
    "left": "Left",
    "right": "Right",
    "home": "Home",
    "end": "End",
    "pageup": "Page_Up",
    "page_up": "Page_Up",
    "pagedown": "Page_Down",
    "page_down": "Page_Down",
    "insert": "Insert",
    "capslock": "Caps_Lock",
    "printscreen": "Print",
}
_BUTTONS = {"left": 1, "middle": 2, "right": 3}
_WHEEL = {"down": 5, "up": 4, "left": 6, "right": 7}


@dataclass
class DesktopState:
    installed: bool
    running: bool
    failed: bool = False
    log: str = ""


class InstaBoxClient:
    name = "instabox"

    def __init__(
        self,
        api_key: str,
        *,
        template: str = "agent-browser",
        base: str = API_BASE,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float = 60.0,
    ) -> None:
        self._client = httpx.AsyncClient(
            headers={"Authorization": f"Bearer {api_key}"},
            transport=transport,
            timeout=timeout,
        )
        self._base = base.rstrip("/")
        self._template = template

    async def aclose(self) -> None:
        await self._client.aclose()

    # -- the machine itself ------------------------------------------------

    async def create_machine(self, *, name: str, cpu: int, auto_stop_minutes: int) -> MachineInfo:
        body: dict[str, Any] = {
            "name": name,
            "templateId": self._template,
            "autoSuspendAfterSeconds": auto_stop_minutes * 60,
            "metadata": {"created_by": "nanomuse"},
        }
        data = await self._json(
            "POST",
            f"{self._base}/v1/machines",
            json=body,
            headers={"Idempotency-Key": str(uuid.uuid4())},
        )
        machine_id = str(data.get("machineId") or data.get("id") or "")
        operation_id = (
            str(data.get("id") or "") if data.get("state") not in (None, "running") else ""
        )
        if operation_id and machine_id and operation_id != machine_id:
            await self._await_operation(operation_id)
        if not machine_id:
            raise CloudError("instabox create answered without a machine id")
        info = await self.get_machine(machine_id)
        if info.state != "running":
            info = await self._await_state(machine_id, {"running"})
        return info

    async def get_machine(self, machine_id: str) -> MachineInfo:
        return self._info(await self._json("GET", f"{self._base}/v1/machines/{machine_id}"))

    async def delete_machine(self, machine_id: str) -> None:
        await self._json(
            "DELETE",
            f"{self._base}/v1/machines/{machine_id}",
            headers={
                "Idempotency-Key": str(uuid.uuid4()),
                "X-Confirm-Delete": machine_id,
            },
            allow_empty=True,
        )
        for _ in range(60):
            try:
                await self.get_machine(machine_id)
            except CloudError as exc:
                if exc.status == 404:
                    return
                raise
            await asyncio.sleep(2)
        raise CloudError(f"instabox machine {machine_id} still exists after the delete")

    async def machine_usage(self, machine_id: str) -> dict[str, Any]:
        """The provider's own metered usage for one machine (not in the
        protocol: the reconciliation reads it, the tools do not)."""
        return await self._json("GET", f"{self._base}/v1/machines/{machine_id}/usage")

    # -- work inside one machine ------------------------------------------

    async def execute(self, machine_id: str, command: str, timeout: float) -> tuple[int, str]:
        return await self._run(machine_id, command, timeout)

    async def _run(
        self, machine_id: str, command: str, timeout: float, stdin: str | None = None
    ) -> tuple[int, str]:
        await self._ensure_running(machine_id)
        body: dict[str, Any] = {"cmd": command, "timeoutSeconds": int(timeout)}
        if stdin is not None:
            body["stdin"] = stdin
        data = await self._json("POST", f"{self._base}/v1/machines/{machine_id}/exec", json=body)
        out = str(data.get("stdout", ""))
        err = str(data.get("stderr", ""))
        if err:
            out = f"{out}\n{err}" if out else err
        return int(data.get("exitCode", 0)), out

    async def upload(self, machine_id: str, remote_path: str, data: bytes) -> None:
        await self._ensure_running(machine_id)
        response = await self._request(
            "PUT",
            f"{self._base}/v1/machines/{machine_id}/files",
            params={"path": remote_path, "parents": "true"},
            content=data,
            headers={"Content-Type": "application/octet-stream"},
        )
        self._raise_for_status(response, "upload")

    async def download(self, machine_id: str, remote_path: str) -> bytes:
        await self._ensure_running(machine_id)
        response = await self._request(
            "GET",
            f"{self._base}/v1/machines/{machine_id}/files/content",
            params={"path": remote_path},
        )
        self._raise_for_status(response, "download")
        return response.content

    async def screenshot(self, machine_id: str) -> bytes:
        await self._ensure_desktop(machine_id)
        path = f"/tmp/instabox-screenshot-{uuid.uuid4().hex[:12]}.png"
        command = (
            f"export DISPLAY={DESKTOP_DISPLAY}; "
            f"if command -v scrot >/dev/null; then scrot -o {path}; "
            f"elif command -v import >/dev/null; then import -window root png:{path}; "
            "else echo 'no screenshot tool in the machine (scrot or ImageMagick import)' "
            ">&2; exit 127; fi"
        )
        code, out = await self._run(machine_id, command, 60)
        if code != 0:
            raise CloudError(f"instabox screenshot failed (exit {code}): {out.strip()[:200]}")
        try:
            return await self.download(machine_id, path)
        finally:
            try:
                await self._run(machine_id, f"rm -f {path}", 30)
            except CloudError:
                pass

    # -- the desktop ---------------------------------------------------------

    async def _desktop_call(self, machine_id: str, body: dict[str, Any]) -> DesktopState:
        data = await self._json("POST", f"{self._base}/v1/machines/{machine_id}/desktop", json=body)
        return DesktopState(
            installed=bool(data.get("installed")),
            running=bool(data.get("running")),
            failed=bool(data.get("failed")),
            log=str(data.get("log") or ""),
        )

    async def desktop_status(self, machine_id: str) -> DesktopState:
        return await self._desktop_call(machine_id, {"action": "status"})

    async def desktop_start(
        self, machine_id: str, width: int = 1280, height: int = 800
    ) -> DesktopState:
        """Start the desktop, installing it first when the template has none.

        Mirrors the provider SDK: a start on a machine without the desktop
        answers installed false, so install runs and status is polled
        until it lands (about half a minute) or reports failure.
        """
        state = await self.desktop_status(machine_id)
        if state.running:
            return state
        if not state.installed:
            if state.failed:
                raise CloudError(f"instabox desktop install failed earlier: {state.log[:200]}")
            state = await self._desktop_call(machine_id, {"action": "install"})
            for _ in range(100):
                if state.installed or state.failed:
                    break
                await asyncio.sleep(3)
                state = await self.desktop_status(machine_id)
            if state.failed:
                raise CloudError(f"instabox desktop install failed: {state.log[:200]}")
            if not state.installed:
                raise CloudError("instabox desktop did not finish installing in time")
        state = await self._desktop_call(
            machine_id, {"action": "start", "width": width, "height": height}
        )
        if not state.running:
            for _ in range(20):
                await asyncio.sleep(2)
                state = await self.desktop_status(machine_id)
                if state.running:
                    break
        if not state.running:
            raise CloudError("instabox desktop did not start")
        return state

    async def _ensure_desktop(self, machine_id: str) -> None:
        state = await self.desktop_status(machine_id)
        if not state.running:
            await self.desktop_start(machine_id)

    async def _xdo(self, machine_id: str, args: str, stdin: str | None = None) -> str:
        code, out = await self._run(
            machine_id, f"export DISPLAY={DESKTOP_DISPLAY}; xdotool {args}", 60, stdin=stdin
        )
        if code != 0:
            raise CloudError(f"instabox desktop action failed (exit {code}): {out.strip()[:200]}")
        return out

    async def desktop_click(
        self, machine_id: str, x: int, y: int, button: str = "left", double: bool = False
    ) -> None:
        number = _BUTTONS.get(button)
        if number is None:
            raise CloudError(f"button must be left, right or middle; got {button!r}")
        repeat = " --repeat 2 --delay 80" if double else ""
        await self._ensure_desktop(machine_id)
        await self._xdo(machine_id, f"mousemove {x} {y} click{repeat} {number}")

    async def desktop_move(self, machine_id: str, x: int, y: int) -> None:
        await self._ensure_desktop(machine_id)
        await self._xdo(machine_id, f"mousemove {x} {y}")

    async def desktop_drag(self, machine_id: str, x: int, y: int, x2: int, y2: int) -> None:
        await self._ensure_desktop(machine_id)
        await self._xdo(
            machine_id,
            f"mousemove {x} {y} mousedown 1 sleep 0.1 mousemove {x2} {y2} sleep 0.1 mouseup 1",
        )

    async def desktop_scroll(
        self, machine_id: str, x: int, y: int, direction: str = "down", amount: int = 3
    ) -> None:
        wheel = _WHEEL.get(direction)
        if wheel is None:
            raise CloudError(f"direction must be up, down, left or right; got {direction!r}")
        if not 1 <= amount <= 100:
            raise CloudError(f"amount must be a whole number from 1 to 100; got {amount}")
        await self._ensure_desktop(machine_id)
        await self._xdo(machine_id, f"mousemove {x} {y} click --repeat {amount} --delay 30 {wheel}")

    async def desktop_type(self, machine_id: str, text: str) -> None:
        if not text:
            return
        await self._ensure_desktop(machine_id)
        # The text travels as the exec call's stdin, never inside the
        # command string, so quotes and newlines need no escaping at all.
        await self._xdo(machine_id, "type --delay 12 --file -", stdin=text)

    async def desktop_key(self, machine_id: str, keys: list[str]) -> None:
        if not keys:
            raise CloudError("a key action needs at least one key")
        chord = "+".join(_XDO_KEYS.get(k.lower(), k) for k in keys)
        await self._ensure_desktop(machine_id)
        await self._xdo(machine_id, f"key -- {shlex.quote(chord)}")

    async def desktop_open_url(self, machine_id: str, url: str) -> None:
        if not (url.startswith("https://") or url.startswith("http://")):
            raise CloudError(f"only http and https URLs open in the browser; got {url!r}")
        await self._ensure_desktop(machine_id)
        command = (
            f"export DISPLAY={DESKTOP_DISPLAY}; "
            "b=$(command -v chromium || command -v chromium-browser || "
            "command -v google-chrome) || { echo 'no browser in the machine' >&2; exit 127; }; "
            'setsid nohup "$b" --no-first-run --no-default-browser-check '
            f"--password-store=basic {shlex.quote(url)} >/dev/null 2>&1 </dev/null &"
        )
        code, out = await self._run(machine_id, command, 60)
        if code != 0:
            raise CloudError(
                f"instabox could not open the browser (exit {code}): {out.strip()[:200]}"
            )

    # -- state helpers ------------------------------------------------------

    async def _ensure_running(self, machine_id: str) -> None:
        info = await self.get_machine(machine_id)
        if info.state == "running":
            return
        action = "start" if info.state == "stopped" else "resume"
        await self._json(
            "POST",
            f"{self._base}/v1/machines/{machine_id}/{action}",
            json={},
            headers={"Idempotency-Key": str(uuid.uuid4())},
            allow_empty=True,
        )
        await self._await_state(machine_id, {"running"})

    async def _await_state(self, machine_id: str, wanted: set[str]) -> MachineInfo:
        for _ in range(90):
            info = await self.get_machine(machine_id)
            if info.state in wanted:
                return info
            await asyncio.sleep(2)
        raise CloudError(f"instabox machine {machine_id} did not reach {sorted(wanted)} in time")

    async def _await_operation(self, operation_id: str) -> None:
        for _ in range(150):
            data = await self._json("GET", f"{self._base}/v1/operations/{operation_id}")
            state = str(data.get("state", ""))
            if state == "succeeded":
                return
            if state in ("failed", "cancelled"):
                error = data.get("error") or {}
                reason = error.get("message") if isinstance(error, dict) else str(error)
                raise CloudError(f"instabox operation {operation_id} failed: {reason}")
            await asyncio.sleep(2)
        raise CloudError(f"instabox operation {operation_id} did not finish in time")

    @staticmethod
    def _info(data: dict[str, Any]) -> MachineInfo:
        spec = data.get("spec") or {}
        return MachineInfo(
            id=str(data.get("id", "")),
            name=str(data.get("name", "")),
            cpu=int(spec.get("vcpus", data.get("vcpus", 4))),
            memory=int(int(spec.get("memMiB", 8192)) / 1024),
            disk=int(spec.get("persistentDiskGiB", 20)),
            state=str(data.get("state", "unknown")),
            detail=str(data.get("templateId") or data.get("image") or ""),
        )

    # -- plumbing ---------------------------------------------------------

    async def _request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        try:
            return await self._client.request(method, url, **kwargs)
        except httpx.HTTPError as exc:
            raise CloudError(f"instabox request failed: {exc.__class__.__name__}") from exc

    def _raise_for_status(self, response: httpx.Response, what: str) -> None:
        if response.status_code >= 400:
            code, message = "", response.text[:200].replace("\n", " ")
            try:
                error = response.json().get("error", {})
                code = str(error.get("code", ""))
                message = str(error.get("message", message))
            except ValueError:
                pass
            label = f"{code}: {message}" if code else message
            raise CloudError(
                f"instabox {what} failed ({response.status_code}): {label}", response.status_code
            )

    async def _json(
        self, method: str, url: str, *, allow_empty: bool = False, **kwargs: Any
    ) -> dict[str, Any]:
        response = await self._request(method, url, **kwargs)
        self._raise_for_status(response, method.lower())
        if allow_empty and not response.content:
            return {}
        try:
            data = response.json()
        except ValueError:
            return {}
        return data if isinstance(data, dict) else {}
