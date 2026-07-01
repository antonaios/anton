"""Crew subprocess launcher + JSON-over-stdio demuxer (#31).

The ONLY way the bridge talks to a MetaGPT crew. Launches
``<crew venv python> -m <crew_module>`` with the ``CrewInput`` JSON on stdin
and consumes line-delimited JSON envelopes on stdout:

  * ``{"_kind": "human_input_required", "msg_id": ..., "prompt": ...}`` —
    a mid-crew HumanProvider ask. Forwarded to the ``on_human_input``
    callback; its return value is written back to the crew's stdin as a
    ``human_input_reply`` envelope and the crew unblocks. (No v1 crew uses
    this — the demuxer ships now so #32/#33/#36 never revisit the boundary;
    METAGPT-INTEGRATION-SPEC.md §3.5.)
  * a line with NO ``_kind`` tag and a ``status`` field — the final
    ``CrewOutput`` result line. Captured and returned.

Fault contract (spec §2.4): stderr is fatal-only — ANY stderr output is a
fault, on any exit code; stdout is line-delimited JSON envelopes ONLY (junk
is a hard protocol violation). A non-zero exit is a fault UNLESS the crew
wrote a structured result line with a non-ok status first (spec §2.4 row 3:
exit 1 + ``CrewOutput(status="error")`` on stdout returns the structured
error so the audit keeps summary/roles/tokens). Crews log debug telemetry to
files under ``crews/.logs/``, never to stderr.

Adaptations vs the staged sketch (``bridge_proxy_pattern.py``), each flagged
in the #31 morning brief:
  * The sketch's wall-clock check sat INSIDE ``for line in proc.stdout`` —
    a silently-hung crew that writes nothing would never trip the deadline.
    Fixed with a reader-thread + ``queue.Queue`` so the deadline fires on
    silence too (Windows has no ``select()`` on pipes).
  * stderr is drained on a side thread — the sketch read it only after
    ``wait()``, which deadlocks if a crashing crew fills the 64KB pipe
    buffer with a traceback while we're blocked on stdout.
  * ``PYTHONIOENCODING=utf-8`` / ``PYTHONUTF8=1`` in the child env — same
    Windows cp1252 mitigation the #26a Synapse installer needed.
"""

from __future__ import annotations

import json
import logging
import os
import queue
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Callable

from pydantic import ValidationError

from routines.crew.pid_store import pid_store
from routines.crew.types import CrewOutput

logger = logging.getLogger(__name__)


# ────────────────────────────────────────────────────────────────────────────
# Constants — operator-tunable via env.
# ────────────────────────────────────────────────────────────────────────────

CREW_PYTHON = Path(os.environ.get(
    "ANTON_CREW_PYTHON",
    r"<repo>\crews\.venv\Scripts\python.exe",
))
CREW_DIR = Path(os.environ.get(
    "ANTON_CREW_DIR",
    r"<repo>\crews",
))
# 10 minutes — outer bound per [[autonomous-crews]] §1 cost-cap rows.
WALL_CLOCK_TIMEOUT_S = int(os.environ.get("ANTON_CREW_TIMEOUT_S", "600"))


# ────────────────────────────────────────────────────────────────────────────
# Exceptions
# ────────────────────────────────────────────────────────────────────────────


class CrewSubprocessError(RuntimeError):
    """Crew subprocess violated the boundary contract — non-zero exit,
    stderr output, invalid/missing result JSON, or a mid-run protocol
    violation. The route maps this to an ``error`` audit row."""


class CrewTimeoutError(RuntimeError):
    """Crew exceeded the wall-clock budget and was killed. The route maps
    this to a ``timeout`` audit row."""


# ────────────────────────────────────────────────────────────────────────────
# Availability probe
# ────────────────────────────────────────────────────────────────────────────


def crew_venv_available() -> bool:
    """True if the isolated crew venv exists (installed per
    ``routines/crew/install/install_metagpt.py``). Routes use this to 503
    cleanly instead of crashing on a missing interpreter."""
    return CREW_PYTHON.is_file()


# ────────────────────────────────────────────────────────────────────────────
# Launcher + demuxer
# ────────────────────────────────────────────────────────────────────────────


# Env vars the crew child ACTUALLY needs (codex-5.5 SEV-2, 2026-06-10): the
# staged sketch inherited the full bridge environment, which would hand every
# provider API key in the bridge's env to the MetaGPT subprocess — exactly the
# leak the local-only lane policy exists to prevent. Allowlist instead:
# OS/runtime plumbing only; the crew gets its LLM endpoint via CrewInput.
_ENV_ALLOWLIST = frozenset({
    # Windows process plumbing
    "SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT", "PATH", "SYSTEMDRIVE",
    "TEMP", "TMP", "USERPROFILE", "APPDATA", "LOCALAPPDATA", "PROGRAMDATA",
    "NUMBER_OF_PROCESSORS", "PROCESSOR_ARCHITECTURE", "OS",
    # POSIX equivalents (POSIX dev boxes / CI)
    "HOME", "LANG", "LC_ALL", "TMPDIR", "USER",
    # TLS trust roots, if the host pins them
    "SSL_CERT_FILE", "REQUESTS_CA_BUNDLE",
})


def _child_env(run_id: str, sensitivity: str) -> dict[str, str]:
    """Minimal, allowlisted environment for the crew subprocess."""
    env = {k: v for k, v in os.environ.items() if k.upper() in _ENV_ALLOWLIST}
    env.update({
        "ANTON_CREW_RUN_ID": run_id,
        "ANTON_CREW_SENSITIVITY": sensitivity,
        # Critical: without unbuffered stdio the result line sits in the
        # child's buffer and both sides deadlock.
        "PYTHONUNBUFFERED": "1",
        # Windows cp1252 mitigation (#26a precedent) — MetaGPT logs arrows
        # + emoji; a cp1252 console codec would crash the child at import.
        "PYTHONIOENCODING": "utf-8",
        "PYTHONUTF8": "1",
    })
    return env


def _pump_lines(stream: Any, q: "queue.Queue[str | None]") -> None:
    """Reader thread: push each stdout line into the queue; ``None`` = EOF."""
    try:
        for raw in stream:
            q.put(raw)
    except Exception:  # noqa: BLE001 — stream died with the process; EOF it
        pass
    finally:
        q.put(None)


def _drain_stderr(stream: Any, buf: list[str]) -> None:
    """Reader thread: accumulate stderr so a crashing crew can't deadlock
    on a full pipe buffer while we're blocked on stdout."""
    try:
        for raw in stream:
            buf.append(raw)
    except Exception:  # noqa: BLE001
        pass


def launch_crew(
    crew_module: str,
    crew_input: dict[str, Any],
    run_id: str,
    sensitivity: str,
    *,
    on_human_input: Callable[[dict[str, Any]], str] | None = None,
    timeout_s: float | None = None,
) -> dict[str, Any]:
    """Launch a crew subprocess synchronously; return the parsed final result.

    Raises :class:`CrewTimeoutError` on wall-clock overrun (subprocess is
    killed first) and :class:`CrewSubprocessError` on every other contract
    violation. The caller (route worker thread) owns audit rows + status
    mapping — this function knows nothing about HTTP or audit.

    ``on_human_input`` receives the raw ``human_input_required`` envelope and
    must return the operator's reply string. When ``None`` (the v1 default —
    no current crew asks mid-run), receiving such an envelope is treated as a
    contract violation: kill + raise, rather than deadlocking a crew that
    waits for a reply nobody can deliver.
    """
    budget = float(timeout_s if timeout_s is not None else WALL_CLOCK_TIMEOUT_S)
    deadline = time.monotonic() + budget
    try:
        proc = subprocess.Popen(
            [str(CREW_PYTHON), "-m", crew_module],
            cwd=str(CREW_DIR),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            env=_child_env(run_id, sensitivity),
        )
    except OSError as e:
        raise CrewSubprocessError(
            f"crew launch failed (is the crew venv installed? "
            f"{CREW_PYTHON}): {e}"
        ) from e

    # F-40: register the Popen HANDLE (not the raw PID) — cancel-by-PID races
    # PID reuse; the handle is pinned to this child. See pid_store docstring.
    pid_store.put(run_id, proc)
    stdout_q: "queue.Queue[str | None]" = queue.Queue()
    stderr_buf: list[str] = []
    t_stdout = threading.Thread(
        target=_pump_lines, args=(proc.stdout, stdout_q),
        daemon=True, name=f"crew-stdout-{run_id}",
    )
    t_stdout.start()
    t_stderr = threading.Thread(
        target=_drain_stderr, args=(proc.stderr, stderr_buf),
        daemon=True, name=f"crew-stderr-{run_id}",
    )
    t_stderr.start()

    # Feed the input line from a side thread (codex-5.5 SEV-2): a blocking
    # pipe write in THIS thread would sit outside the deadline loop — a child
    # that never reads stdin (or an input larger than the pipe buffer) would
    # hang the worker forever. The writer thread can block harmlessly; the
    # deadline loop below stays authoritative and the kill unblocks it.
    # No write interleaving with human-input replies: the child cannot ASK
    # before it has READ the input line, so the feed always completes first.
    def _feed_stdin() -> None:
        try:
            proc.stdin.write(json.dumps(crew_input) + "\n")
            proc.stdin.flush()
        except (OSError, ValueError):
            # OSError: child died before/while reading input (e.g. import
            # error in the crew venv) — the main loop sees EOF + returncode
            # and reports. ValueError: the finally-block closed stdin while
            # this thread was still blocked on the write (kill/timeout race).
            pass

    threading.Thread(
        target=_feed_stdin, daemon=True, name=f"crew-stdin-{run_id}",
    ).start()

    def _kill() -> None:
        try:
            proc.kill()
            proc.wait(timeout=10)
        except Exception:  # noqa: BLE001 — already dead / unkillable; move on
            pass

    final_result: dict[str, Any] | None = None
    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                _kill()
                raise CrewTimeoutError(
                    f"crew {crew_module!r} exceeded {budget:.0f}s wall clock"
                )
            try:
                line = stdout_q.get(timeout=min(remaining, 1.0))
            except queue.Empty:
                continue  # re-check deadline
            if line is None:
                break  # EOF — child closed stdout
            line = line.strip()
            if not line:
                continue
            try:
                envelope = json.loads(line)
            except json.JSONDecodeError:
                # Strict protocol (codex-5.5 SEV-2): stdout is line-delimited
                # JSON envelopes ONLY. Tolerating junk would let leaked logs /
                # stray prints hide inside a "healthy" run — fail loudly so a
                # mis-routed logger is caught by the smoke test. (Crew-side,
                # boundary.capture_protocol_stream redirects stray prints to
                # a file so well-behaved crews can always satisfy this.)
                _kill()
                raise CrewSubprocessError(
                    f"crew {crew_module!r} wrote non-JSON to stdout "
                    f"(line-delimited JSON contract): {line[:200]!r}"
                )
            if not isinstance(envelope, dict):
                _kill()
                raise CrewSubprocessError(
                    f"crew {crew_module!r} wrote a non-object JSON line to "
                    f"stdout: {line[:200]!r}"
                )
            kind = envelope.get("_kind")
            if kind == "human_input_required":
                if on_human_input is None:
                    _kill()
                    raise CrewSubprocessError(
                        f"crew {crew_module!r} asked for human input but the "
                        f"caller provided no on_human_input handler"
                    )
                # Deadline-aware ask (codex-5.5 xhigh, 2026-06-10): the
                # callback blocks on the operator (up to the route's reply
                # timeout); calling it inline would suspend the wall clock
                # and let a HITL run exceed cost_cap_seconds. Run it on a
                # side thread and keep THIS loop's deadline authoritative.
                ask_box: dict[str, Any] = {}

                def _ask(env: dict[str, Any] = envelope) -> None:
                    try:
                        ask_box["reply"] = on_human_input(env)
                    except BaseException as e:  # noqa: BLE001 — re-raised below
                        ask_box["error"] = e

                t_ask = threading.Thread(
                    target=_ask, daemon=True, name=f"crew-ask-{run_id}",
                )
                t_ask.start()
                t_ask.join(timeout=max(0.0, deadline - time.monotonic()))
                if t_ask.is_alive():
                    _kill()
                    raise CrewTimeoutError(
                        f"crew {crew_module!r} exceeded {budget:.0f}s wall "
                        f"clock while waiting for a human-input reply"
                    )
                if "error" in ask_box:
                    raise ask_box["error"]
                proc.stdin.write(json.dumps({
                    "_kind": "human_input_reply",
                    "msg_id": envelope.get("msg_id"),
                    "response": ask_box["reply"],
                }) + "\n")
                proc.stdin.flush()
            elif kind is None and "status" in envelope:
                # Validate against the bridge-side contract BEFORE trusting
                # it (codex-5.5 xhigh, 2026-06-10): a malformed result would
                # otherwise be audited as-is and 500 later in get_crew_run.
                try:
                    final_result = CrewOutput.model_validate(envelope).model_dump()
                except ValidationError as e:
                    _kill()
                    raise CrewSubprocessError(
                        f"crew {crew_module!r} result line failed CrewOutput "
                        f"validation: {e.errors()[:3]!r}"
                    ) from e
                break
            else:
                # Unknown envelope kind — log + continue (future-extensible).
                logger.warning(
                    "crew %s/%s: unknown stdout envelope kind=%r",
                    crew_module, run_id, kind,
                )

        # Wait for exit within what's left of the budget.
        try:
            proc.wait(timeout=max(0.1, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            _kill()
            raise CrewTimeoutError(
                f"crew {crew_module!r} wrote its result but did not exit "
                f"within {budget:.0f}s wall clock"
            ) from None

        # Join the pump threads BEFORE judging stderr/stdout (codex-5.5
        # SEV-2): the child has exited, but its last pipe writes may still be
        # in flight on the reader threads — judging early makes the
        # fatal-stderr contract flaky.
        t_stderr.join(timeout=5)
        t_stdout.join(timeout=5)

        stderr_text = "".join(stderr_buf)
        if stderr_text:
            # Spec §2.4: stderr non-empty is ALWAYS a fault signal — on any
            # exit code. Crews log to files, never stderr; fail loudly so a
            # mis-configured logger is caught by the smoke test, not in prod.
            raise CrewSubprocessError(
                f"crew {crew_module!r} wrote to stderr (fatal-only contract; "
                f"rc={proc.returncode}); first 2000 chars: {stderr_text[:2000]}"
            )
        # Drain whatever arrived AFTER the result line (codex-5.5 xhigh,
        # 2026-06-10): the contract is one result line, then EOF. Stopping
        # the read loop at the result let trailing junk — leaked logs, a
        # second result line, stray envelopes — slip past the strict-stdout
        # contract unjudged.
        trailing: list[str] = []
        while True:
            try:
                left = stdout_q.get_nowait()
            except queue.Empty:
                break
            if left is not None and left.strip():
                trailing.append(left.strip())
        if trailing:
            raise CrewSubprocessError(
                f"crew {crew_module!r} wrote {len(trailing)} stdout line(s) "
                f"after its result line (one-result contract); first: "
                f"{trailing[0][:200]!r}"
            )
        if final_result is None:
            raise CrewSubprocessError(
                f"crew {crew_module!r} exited {proc.returncode} without "
                f"writing a result line"
            )
        if proc.returncode != 0:
            # Spec §2.4 row 3 (codex-5.5 SEV-2): a STRUCTURED error — clean
            # stderr + a valid result line with a non-ok status + exit 1 —
            # returns the result so the audit keeps summary/roles/tokens.
            # A non-zero exit claiming status="ok" is still a fault.
            if final_result.get("status") in ("error", "cancelled", "timeout"):
                return final_result
            raise CrewSubprocessError(
                f"crew {crew_module!r} exited {proc.returncode} but its "
                f"result line claims status="
                f"{final_result.get('status')!r} — contract violation"
            )
        return final_result
    finally:
        pid_store.pop(run_id)
        try:
            if proc.stdin and not proc.stdin.closed:
                proc.stdin.close()
        except Exception:  # noqa: BLE001
            pass
        # No orphans, EVER: any exit path that leaves the child alive
        # (e.g. the on_human_input callback raised on operator-reply
        # timeout) must reap it — a forgotten crew would keep burning
        # Ollama tokens with nobody listening.
        if proc.poll() is None:
            _kill()


__all__ = [
    "CREW_PYTHON",
    "CREW_DIR",
    "WALL_CLOCK_TIMEOUT_S",
    "CrewSubprocessError",
    "CrewTimeoutError",
    "crew_venv_available",
    "launch_crew",
]
