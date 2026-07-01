"""ANTONHumanProvider — MetaGPT HumanProvider over the stdio boundary (#31).

MetaGPT ships ``HumanProvider`` (a fake LLM that calls ``input()`` for
keyboard-driven HITL). Useless from a subprocess — no controlling terminal;
the dashboard is the operator's UI. This subclass routes the ask through the
JSON-over-stdio boundary instead:

  1. A Role's ``llm.aask(prompt)`` lands here.
  2. We write ``{"_kind": "human_input_required", "msg_id": …, "prompt": …}``
     to stdout (single line) and flush.
  3. The bridge demuxer SSE-pushes it to the dashboard; the operator answers
     via ``POST /api/crew/runs/<id>/human-input``.
  4. The bridge writes ``{"_kind": "human_input_reply", "msg_id": …,
     "response": …}`` to our stdin; we block on ``readline()`` until the
     matching msg_id arrives, then return the response as the "completion".

No v1 crew uses this mid-run (METAGPT-INTEGRATION-SPEC.md §3.5) — it ships
with #31 so future crews don't force a boundary-contract rev. The bridge-side
import-and-loads check is the smoke: an import-time error here would block
adding such crews later.

Module-level metagpt import is deliberate (we must subclass) — this module
only ever loads inside the crew venv.
"""

from __future__ import annotations

import json
import sys
import uuid
from typing import Any

from metagpt.provider.human_provider import HumanProvider as _Base

from _shared.boundary import protocol_stream


class ANTONHumanProvider(_Base):
    """Replaces the CLI-blocking ``input()`` with a JSON-stdio handshake."""

    async def aask(self, msg: str, **kwargs: Any) -> str:
        msg_id = uuid.uuid4().hex[:8]
        envelope = {
            "_kind": "human_input_required",
            "msg_id": msg_id,
            "prompt": msg,
            "context": kwargs.get("context", {}),
        }
        # Protocol stream, not sys.stdout — stray prints are redirected away
        # from the boundary by boundary.capture_protocol_stream().
        out = protocol_stream()
        out.write(json.dumps(envelope) + "\n")
        out.flush()
        # Block until the bridge writes the matching reply line on stdin.
        while True:
            line = sys.stdin.readline()
            if not line:
                raise RuntimeError("stdin closed before human reply arrived")
            try:
                reply = json.loads(line)
            except json.JSONDecodeError:
                continue
            if (
                isinstance(reply, dict)
                and reply.get("_kind") == "human_input_reply"
                and reply.get("msg_id") == msg_id
            ):
                return str(reply.get("response", ""))


__all__ = ["ANTONHumanProvider"]
