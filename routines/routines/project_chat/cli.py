"""CLI for project chat — ``project-chat ask <DEAL> "<question>"``.

Two commands:

    project-chat ask <DEAL> "<question>"   — run one chat turn, persist, print
    project-chat history <DEAL>            — print the stored conversation

``ask`` runs the full turn pipeline (the shared :func:`run_turn` helper that
the bridge endpoint also uses) so the CLI and the route can't drift. The
local Ollama model is the only LLM lane the CLI uses — confidential deals stay
local by construction.
"""

from __future__ import annotations

import logging
import sys
import time
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import click

from routines.project_chat.pull import gather_context
from routines.project_chat.reader import load_history
from routines.project_chat.schema import ChatResponse
from routines.project_chat.synthesise import DEFAULT_MODEL, answer, answer_stream, make_turns
from routines.project_chat.writer import append_turns
from routines.shared.ollama_client import OllamaClient

DEFAULT_VAULT = Path("/mnt/x/OS AI Vault")


def run_turn(
    vault_root: Path,
    project: str,
    message: str,
    *,
    client: OllamaClient,
    history_turns: int = 6,
    recall_limit: int = 8,
    model: str = DEFAULT_MODEL,
    now_iso: str | None = None,
    cross_projects: bool = False,
) -> ChatResponse:
    """Run one full chat turn and persist it. Shared by the CLI + the bridge.

    Steps (mirrors the plan §6.6 endpoint behaviour, minus the audit write
    which the route owns): gather context (history + sensitivity + recall) →
    call the LLM → build the (user, assistant) turn pair → atomic-append both
    to ``_chat.md`` → return the ``ChatResponse``.

    ``cross_projects`` (default OFF) widens recall to the whole vault under the
    ``≤ internal`` out-of-deal cap (see ``pull.fetch_sources``); it is echoed on
    the returned ``ChatResponse`` so the caller can mark the turn cross-scope.
    """
    started = time.monotonic()
    ts = now_iso or datetime.now(timezone.utc).isoformat(timespec="seconds")

    ctx = gather_context(
        vault_root, project, message,
        client=client, history_turns=history_turns, recall_limit=recall_limit,
        cross_projects=cross_projects,
    )
    answer_text = answer(ctx, client=client, model=model)
    user_turn, assistant_turn = make_turns(ctx, answer_text, now_iso=ts)
    append_turns(
        vault_root, project, [user_turn, assistant_turn],
        sensitivity=ctx.sensitivity,
    )

    duration_ms = int((time.monotonic() - started) * 1000)
    return ChatResponse(
        turn=assistant_turn,
        sources=list(ctx.sources),
        recall_hits=len(ctx.sources),
        duration_ms=duration_ms,
        cross_projects=cross_projects,
    )


@dataclass(frozen=True)
class StreamDelta:
    """One incremental answer chunk emitted while the model is generating."""

    text: str


@dataclass(frozen=True)
class StreamDone:
    """Terminal event: the turn finished + persisted. Carries the same
    ``ChatResponse`` :func:`run_turn` returns (assistant turn + recall stats)."""

    response: ChatResponse


def run_turn_stream(
    vault_root: Path,
    project: str,
    message: str,
    *,
    client: OllamaClient,
    history_turns: int = 6,
    recall_limit: int = 8,
    model: str = DEFAULT_MODEL,
    now_iso: str | None = None,
    cross_projects: bool = False,
) -> Iterator[StreamDelta | StreamDone]:
    """Streaming sibling of :func:`run_turn` — the shared turn pipeline for the
    SSE chat endpoint. Yields a :class:`StreamDelta` per token, then a single
    :class:`StreamDone` once both turns are persisted.

    The persistence step (``append_turns``) runs ONLY after the model stream is
    fully consumed — after the last ``StreamDelta`` is yielded and before
    ``StreamDone``. A partial/truncated turn is therefore NEVER persisted:

      * The answer is guaranteed COMPLETE at the write —
        ``OllamaClient.chat_stream`` raises
        :class:`~routines.shared.ollama_client.OllamaError` if the model stream
        ends before its terminating ``done`` frame, so a truncated stream
        propagates out of the delta loop (and out of this generator) BEFORE
        ``append_turns`` is reached. An empty completion is likewise rejected.
      * The generator is demand-driven: if the caller stops iterating during the
        deltas (a client disconnect closes the generator at a delta yield),
        control never reaches ``append_turns`` and nothing is written. (A
        disconnect at the very last ``delta``→``StreamDone`` boundary may still
        complete the write — but only of the COMPLETE answer, never a partial
        one; the route records that as a successful turn, not ``cancelled``.)

    Idempotency, the per-path write lock, and the fail-closed corrupt-log guard
    all come from ``writer.append_turns`` unchanged — streaming does NOT bypass
    the writer.
    """
    started = time.monotonic()
    ts = now_iso or datetime.now(timezone.utc).isoformat(timespec="seconds")

    ctx = gather_context(
        vault_root, project, message,
        client=client, history_turns=history_turns, recall_limit=recall_limit,
        cross_projects=cross_projects,
    )

    parts: list[str] = []
    for delta in answer_stream(ctx, client=client, model=model):
        parts.append(delta)
        yield StreamDelta(delta)

    # Stream consumed cleanly → assemble + atomic-append BOTH turns. An empty
    # completion is treated as a failure (no half/empty turn persisted).
    answer_text = "".join(parts).strip()
    if not answer_text:
        from routines.shared.ollama_client import OllamaError
        raise OllamaError("local model returned an empty response")
    user_turn, assistant_turn = make_turns(ctx, answer_text, now_iso=ts)
    append_turns(
        vault_root, project, [user_turn, assistant_turn],
        sensitivity=ctx.sensitivity,
    )

    duration_ms = int((time.monotonic() - started) * 1000)
    yield StreamDone(ChatResponse(
        turn=assistant_turn,
        sources=list(ctx.sources),
        recall_hits=len(ctx.sources),
        duration_ms=duration_ms,
        cross_projects=cross_projects,
    ))


@click.group()
@click.option("--debug", is_flag=True)
def main(debug: bool) -> None:
    """Project chat — per-deal conversational memory via local Ollama."""
    logging.basicConfig(
        level=logging.DEBUG if debug else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


@main.command("ask")
@click.argument("deal")
@click.argument("question")
@click.option("--vault", type=click.Path(exists=True, file_okay=False, path_type=Path),
              default=DEFAULT_VAULT)
@click.option("--history-turns", type=int, default=6, show_default=True)
@click.option("--recall-limit", type=int, default=8, show_default=True)
@click.option("--ollama-url", default="http://127.0.0.1:11434")
@click.option("--model", default=DEFAULT_MODEL)
@click.option("--cross-projects/--no-cross-projects", default=False, show_default=True,
              help="Widen recall to the whole vault (out-of-deal capped at ≤ internal).")
def ask_cmd(
    deal: str, question: str, vault: Path,
    history_turns: int, recall_limit: int, ollama_url: str, model: str,
    cross_projects: bool,
) -> None:
    """Ask Anton a question about DEAL; persist the turn to its _chat.md."""
    if not question.strip():
        click.echo("error: question is empty", err=True)
        sys.exit(1)

    client = OllamaClient(base_url=ollama_url)
    resp = run_turn(
        vault, deal, question,
        client=client, history_turns=history_turns,
        recall_limit=recall_limit, model=model, cross_projects=cross_projects,
    )

    click.echo(resp.turn.text)
    if resp.sources:
        click.echo(f"\nSources ({resp.recall_hits}):")
        for s in resp.sources:
            click.echo(f"  - {s.path} (score {s.score:.2f})")
    click.echo(f"\n[{resp.duration_ms} ms]")


@main.command("history")
@click.argument("deal")
@click.option("--vault", type=click.Path(exists=True, file_okay=False, path_type=Path),
              default=DEFAULT_VAULT)
def history_cmd(deal: str, vault: Path) -> None:
    """Print DEAL's stored chat history."""
    turns = load_history(vault, deal)
    if not turns:
        click.echo(f"No chat history for {deal}.")
        return
    for t in turns:
        speaker = "you" if t.role == "user" else "anton"
        click.echo(f"## {t.timestamp} · {speaker}")
        click.echo(t.text)
        if t.sources:
            click.echo("Sources:")
            for s in t.sources:
                click.echo(f"  - {s.path} (score {s.score:.2f})")
        click.echo("")


if __name__ == "__main__":
    main()
