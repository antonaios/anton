"""Word-compatible tracked-changes rewriter for ``.docx`` documents.

``TrackedDoc`` wraps a ``python-docx`` :class:`~docx.document.Document` and emits
*native* Word revision markup — ``<w:ins>`` (insertion) and ``<w:del>``
(deletion) runs, plus threaded comments — so the operator opens the saved file
in Microsoft Word and sees real red-line markup with working **Accept / Reject**
and a populated **Reviewing** pane ("ANTON proposed: …"). No Word COM
automation, no third-party service — pure XML manipulation via ``python-docx`` +
``lxml``.

This is the shared engine every future drafting skill leans on (SPA redline,
heads-of-terms markup, IC-memo edits, NDA mark-ups). It deliberately steals only
the *shape* of the pattern surveyed in ``MIKE-EVALUATION-2026-05-26.md`` (pattern
#1); the implementation is original and dependency-light.

Public API
----------
::

    doc = TrackedDoc(base_docx_path)
    doc.insert(at_anchor="the Purchaser", text=" (the \"Buyer\")", author="ANTON")
    doc.delete(anchor_text="best endeavours", author="ANTON")
    doc.replace(find="shall", replace="will", author="ANTON",
                before_context="The Purchaser ", after_context=" pay")
    doc.comment(at_anchor="warranty 7.3", body="Unusually narrow — flag.", author="ANTON")
    doc.save(out_path)

How it works (the Word-XML approach)
------------------------------------
A paragraph's visible text is a *forest* of ``<w:r>`` runs, each holding a
``<w:t>``. To track a change at the XML level we:

1. **Match the anchor** against the paragraph's VISIBLE text — the
   *accept-all-insertions* view: plain ``<w:r>`` runs PLUS runs nested inside a
   pre-existing ``<w:ins>``, but NOT runs inside ``<w:del>`` (struck text). The
   token stream is built in true DOCUMENT order (body children, recursing into
   table cells), whitespace collapsed first (operators never reproduce exact
   whitespace — see :func:`_collapse_ws`). A position map translates a match in the
   normalised string back to exact original character offsets. An edit whose
   matched range touches a pre-existing ``<w:ins>`` run is refused
   (:class:`RevisionOverlap`) rather than cutting into another author's revision.
2. **Split runs at the match boundaries** (:func:`_split_text_run`) so the matched
   text occupies a contiguous sequence of whole ``<w:r>`` elements, each carrying
   the original run's ``<w:rPr>`` (formatting is preserved). For a deletion the
   matched runs must be CONTIGUOUS siblings; if foreign markup sits between them
   the deletion is refused (:class:`RevisionOverlap`) — see :meth:`_wrap_in_del`.
3. **Wrap / inject revision elements**:
     * *delete* → wrap the matched runs in ``<w:del>`` and rename every ``<w:t>``
       to ``<w:delText>`` (Word requires deleted text in ``delText``, not ``t``).
     * *insert* → inject a ``<w:ins>`` carrying a fresh run after the anchor.
     * *replace* → a paired ``<w:del>`` (old) immediately followed by ``<w:ins>``
       (new) at the same anchor.
   Every ``<w:ins>``/``<w:del>`` carries ``w:id`` (unique), ``w:author`` and a
   ``w:date`` ISO-8601 timestamp, so Word attributes the change.
4. **Comments** (:meth:`TrackedDoc.comment`) use ``python-docx``'s native
   ``Document.add_comment`` (1.2+) which materialises ``word/comments.xml`` + the
   relationship + content-type override and brackets the anchor with
   ``<w:commentRangeStart>`` / ``<w:commentRangeEnd>`` / ``<w:commentReference>``.
   Visible body text is unchanged.

Load-bearing guarantees
------------------------
* **Whitespace-tolerant** anchor matching (collapse ``\\s+`` → single space).
* **Context-anchored disambiguation** — ``before_context`` / ``after_context``
  pick the right instance when ``find`` occurs many times ("the Purchaser"
  appears 50× in an SPA).
* **Per-author attribution + timestamp** on every revision.
* **Idempotent** — re-running the same operation (same instance OR on the saved
  output reloaded) does NOT compound edits. Each op detects an
  ``author``-attributed tracked block matching THIS edit and is a no-op. The check
  runs BEFORE any mutation, and is scoped so a re-run never walks on to a *second*
  identical occurrence: ``delete`` keys on (author, struck text); ``replace`` keys
  on (author, find→replace) AND the supplied context, so two genuinely different
  occurrences each still get their own redline; ``insert`` keys on the anchor +
  the EXACT proposed text; ``comment`` keys on (author, body) over the EXACT matched
  range (so the same body can annotate two distinct anchors in one paragraph).
  Anchor text is compared whitespace-tolerantly; PROPOSED text (insert/replace
  payloads) is compared verbatim — ``" Buyer"`` ≠ ``"Buyer"``.
* **Legal numbering preserved** — edits only touch ``<w:r>`` runs *inside* a
  paragraph and never remove the ``<w:p>`` or its ``<w:pPr>``/``<w:numPr>``, so
  auto-numbered clause lists survive (Word re-numbers on Accept).

Known boundaries (documented, not bugs)
---------------------------------------
* An anchor must be contained within a single paragraph (matching does not span
  paragraph marks). Table-cell paragraphs *are* searched (including nested
  tables).
* Splitting a run that mixes ``<w:t>`` with ``<w:tab>``/``<w:br>`` re-tokenizes the
  fragment, PRESERVING those control elements (see :func:`_append_text_content`);
  inserted text carrying ``\\t``/``\\n`` is likewise emitted as ``<w:tab/>``/
  ``<w:br/>`` rather than dead literal characters.
* Paragraph-mark deletion (deleting an entire clause incl. its number) is out of
  scope — only run-level deletes within a paragraph are tracked.
* **Hyperlink runs**: matching does not surface runs nested inside
  ``<w:hyperlink>`` (mirroring python-docx ``Paragraph.runs``). Anchors that fall
  inside hyperlinked text are simply *not found* (a safe ``AnchorNotFound``, never a
  corruption). Anchor on the surrounding plain text instead.
* **Pre-existing revisions**: matching SEES ``<w:ins>`` text (so an anchor cannot
  silently match across an earlier insertion, and context can resolve against
  inserted text) but never struck ``<w:del>`` text. An edit whose anchor overlaps an
  existing ``<w:ins>`` is refused with :class:`RevisionOverlap` — narrow the anchor
  to plain text, or apply this edit before the insertion.
* **Non-text & foreign markup between matched runs**: inline drawings/fields,
  comment-reference runs and comment/bookmark range markers carry no matchable text,
  so they are never *in* a matched range. If one sits physically *between* the
  matched runs, the deletion is REFUSED (:class:`RevisionOverlap`) rather than
  wrapping a non-contiguous span that would orphan the marker — see
  :meth:`_wrap_in_del`.
* **Inline object mixed inside a text run**: the rare ``<w:r>`` that holds text AND
  an inline ``<w:drawing>``/``<w:object>``/field in the same run is flagged
  non-editable (:func:`_run_has_foreign_content`); a matched range touching it is
  REFUSED rather than splitting/striking the run and moving or deleting the object.
* **Comment × edit interaction**: ``insert`` at an already-commented anchor lands
  *outside* the comment range (after its closing markers), so it doesn't re-scope the
  comment. A ``delete``/``replace`` whose span *crosses* a commented region (or any
  foreign marker) is REFUSED with :class:`RevisionOverlap` rather than emitting a
  half-struck, loosely-positioned comment — apply the redline BEFORE the comment, or
  keep the deletion clear of the commented span.
* **Loading**: the source ``.docx`` is opened behind zip-bomb / corrupt-archive
  guards (member-count and total-uncompressed-size caps, read from the central
  directory only — no per-member ratio cap, which would reject legitimately
  high-compression OOXML). A failure raises :class:`DocxLoadError`.

Dependencies: ``python-docx`` + ``lxml`` only (both already vendored).
"""

from __future__ import annotations

import copy
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, NamedTuple
from zipfile import BadZipFile, ZipFile, is_zipfile

from docx import Document
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.table import Table
from docx.text.paragraph import Paragraph

__all__ = ["TrackedDoc", "AnchorNotFound", "RevisionOverlap", "DocxLoadError"]


# --------------------------------------------------------------------------- #
# Qualified names (cached — qn() does a dict lookup each call).
# --------------------------------------------------------------------------- #
_W_R = qn("w:r")
_W_RPR = qn("w:rPr")
_W_T = qn("w:t")
_W_DELTEXT = qn("w:delText")
_W_TAB = qn("w:tab")
_W_BR = qn("w:br")
_W_CR = qn("w:cr")
_W_INS = qn("w:ins")
_W_DEL = qn("w:del")
_W_COMMENT_REF = qn("w:commentReference")
_W_COMMENT_RANGE_START = qn("w:commentRangeStart")
_W_COMMENT_RANGE_END = qn("w:commentRangeEnd")
_W_BOOKMARK_START = qn("w:bookmarkStart")
_W_BOOKMARK_END = qn("w:bookmarkEnd")
_W_PROOF_ERR = qn("w:proofErr")
_W_ID = qn("w:id")
_W_AUTHOR = qn("w:author")
_W_DATE = qn("w:date")
_XML_SPACE = qn("xml:space")

# Markup elements that may sit between an anchor run and a following ``<w:ins>``
# without being "real" content — Word interleaves these (a comment added to the
# same anchor injects a range-end + a reference run). The insert-idempotency check
# must skip past them, else a re-run after a same-anchor comment adds a duplicate.
_ANNOTATION_MARKERS = {
    _W_COMMENT_RANGE_START, _W_COMMENT_RANGE_END,
    _W_BOOKMARK_START, _W_BOOKMARK_END, _W_PROOF_ERR,
}

# Children of a <w:r> that carry visible/textual content (rewritten on split).
_TEXT_BEARING = {_W_T, _W_DELTEXT, _W_TAB, _W_BR, _W_CR}

# A <w:r> is safe to split / strike only if every child is text-bearing content or
# its <w:rPr>. Anything else — an inline <w:drawing>/<w:object>/<w:pict>, a field
# (<w:fldChar>/<w:instrText>/<w:fldSimple>), a <w:commentReference> — is "foreign":
# splitting or striking the run would move or delete that object. A run carrying
# such content is flagged non-editable so any matched range touching it is refused.
_RUN_SAFE_CHILDREN = _TEXT_BEARING | {_W_RPR}


class AnchorNotFound(ValueError):
    """Raised when an anchor / ``find`` string cannot be located in the document
    (and is not already present as a matching tracked change by the same author).
    """


class RevisionOverlap(ValueError):
    """Raised when an edit would have to cut across a *pre-existing* tracked
    change or foreign markup to apply cleanly — e.g. an anchor that overlaps an
    existing ``<w:ins>``, or a deletion whose matched runs are not contiguous
    siblings (a comment / bookmark / revision marker, inline object, or
    hyperlink sits between them). Refusing is deliberate: silently wrapping
    across such markers orphans them and produces a non-contiguous,
    Word-confusing redline. Narrow the anchor to plain text, or apply the
    redline *before* layering comments/insertions over the same span.
    """


# Zip-bomb / corrupt-archive guards applied when loading a ``.docx`` (which is a
# zip). Read from the central directory only (no decompression), so a malicious
# archive is rejected before any member is expanded. We bound the TOTAL declared
# uncompressed size and the member count — these bound the actual expansion a bomb
# can force. We deliberately do NOT use a per-member compression-ratio cap: OOXML is
# repetitive XML that legitimately compresses far past any "suspicious" ratio
# (``word/document.xml`` for a long document routinely exceeds 250:1), so a ratio cap
# rejects valid files; and a real bomb can name its oversized member anything, so a
# name-based exemption is not a security boundary either.
_MAX_ZIP_MEMBERS = 5_000           # a real .docx has tens–hundreds of parts
_MAX_ZIP_TOTAL_BYTES = 300 << 20   # 300 MiB total uncompressed


class DocxLoadError(ValueError):
    """Raised when the source ``.docx`` cannot be safely loaded — not a zip, a
    corrupt archive, a failed ``python-docx`` open, or a file that trips the
    zip-bomb guards (member count / total-size / compression-ratio caps)."""


# --------------------------------------------------------------------------- #
# Whitespace-tolerant normalisation with a position map.
# --------------------------------------------------------------------------- #
_WS_RUN = re.compile(r"\s+")


def _collapse_ws(text: str) -> str:
    """Collapse every run of whitespace to a single space and strip the ends.

    The single cheapest thing that makes ``.docx`` anchoring actually work:
    operators paste ``"the  Purchaser"`` (two spaces) or ``"the\\tPurchaser"``
    against a doc that has one space, and naive ``str.find`` fails. We normalise
    both sides before comparing.
    """
    return _WS_RUN.sub(" ", text).strip()


def _normalize_with_map(text: str) -> tuple[str, list[int], list[int]]:
    """Return ``(norm, starts, ends)`` where ``norm`` is ``text`` with each run of
    whitespace collapsed to a single space (NOT stripped — leading/trailing space
    is kept so positions stay meaningful), and ``starts[i]`` / ``ends[i]`` are the
    original ``[start, end)`` offsets covered by ``norm[i]``.

    A collapsed whitespace run maps its single space to the whole original run, so
    a match that begins/ends on collapsed whitespace still resolves to exact
    original offsets.
    """
    norm: list[str] = []
    starts: list[int] = []
    ends: list[int] = []
    i, n = 0, len(text)
    while i < n:
        if text[i].isspace():
            j = i
            while j < n and text[j].isspace():
                j += 1
            norm.append(" ")
            starts.append(i)
            ends.append(j)
            i = j
        else:
            norm.append(text[i])
            starts.append(i)
            ends.append(i + 1)
            i += 1
    return "".join(norm), starts, ends


# --------------------------------------------------------------------------- #
# Run-element text helpers (operate on raw lxml <w:r> elements).
# --------------------------------------------------------------------------- #
def _run_text(r) -> str:
    """Concatenate the textual content of a ``<w:r>`` element, mirroring how Word
    renders it: ``<w:t>``/``<w:delText>`` → text, ``<w:tab>`` → ``\\t``,
    ``<w:br>``/``<w:cr>`` → ``\\n``."""
    out: list[str] = []
    for child in r:
        tag = child.tag
        if tag == _W_T or tag == _W_DELTEXT:
            out.append(child.text or "")
        elif tag == _W_TAB:
            out.append("\t")
        elif tag == _W_BR or tag == _W_CR:
            out.append("\n")
    return "".join(out)


def _run_has_foreign_content(r) -> bool:
    """True if ``<w:r>`` element ``r`` carries any child beyond text-bearing content
    and its ``<w:rPr>`` — e.g. an inline ``<w:drawing>``/``<w:object>``/``<w:pict>``,
    a field (``<w:fldChar>``/``<w:instrText>``/``<w:fldSimple>``) or a
    ``<w:commentReference>``. Such a run cannot be split or struck without moving or
    deleting that object, so a matched range that touches it is refused
    (:class:`RevisionOverlap`). (A run holding ONLY such an object has no text and is
    never a match span; this guards the rarer run that mixes text WITH one.)"""
    return any(c.tag not in _RUN_SAFE_CHILDREN for c in r)


def _element_text(el) -> str:
    """Canonical concatenated text of every text-bearing descendant of ``el``
    (used to read back an existing ``<w:ins>``/``<w:del>`` block). Mirrors
    :func:`_run_text` EXACTLY — ``<w:t>``/``<w:delText>`` → text, ``<w:tab>`` →
    ``\\t``, ``<w:br>``/``<w:cr>`` → ``\\n`` — so revision-equality checks see the
    same string anchoring used. A deleted run keeps its ``<w:tab>`` as an element
    (``_to_del_text`` only rewrites ``<w:t>``), so this MUST account for tabs/breaks
    or idempotency on tab-bearing deletions silently breaks on reload."""
    out: list[str] = []
    for node in el.iter(_W_T, _W_DELTEXT, _W_TAB, _W_BR, _W_CR):
        tag = node.tag
        if tag == _W_T or tag == _W_DELTEXT:
            out.append(node.text or "")
        elif tag == _W_TAB:
            out.append("\t")
        else:  # _W_BR / _W_CR
            out.append("\n")
    return "".join(out)


def _visible_run_text(elements) -> str:
    """Concatenated VISIBLE text of an iterable of paragraph-level elements — plain
    ``<w:r>`` runs plus runs nested inside ``<w:ins>``, EXCLUDING ``<w:del>`` (struck
    text). This is the same accept-all-insertions view :meth:`TrackedDoc._locate`
    matches against, so the context-idempotency checks
    (:meth:`TrackedDoc._pair_context_ok` / :meth:`TrackedDoc._del_context_ok`) stay
    consistent with it — a ``before``/``after_context`` that references inserted text
    resolves the same way on a re-run as it did on the first pass."""
    out: list[str] = []
    for c in elements:
        if c.tag == _W_R:
            out.append(_run_text(c))
        elif c.tag == _W_INS:
            for r in c:
                if r.tag == _W_R:
                    out.append(_run_text(r))
    return "".join(out)


def _anchor_eq(a: str, b: str) -> bool:
    """Whitespace-tolerant equality — for ANCHOR text (``find`` / ``anchor_text``),
    matched leniently against the doc (operators don't reproduce exact whitespace)."""
    return _collapse_ws(a) == _collapse_ws(b)


def _payload_eq(a: str, b: str) -> bool:
    """EXACT equality — for PROPOSED text (``insert`` text / ``replace`` replacement).
    ``" Buyer"`` and ``"Buyer"`` are DIFFERENT legal edits and must not be treated as
    already-applied; only anchors get whitespace collapse, never payloads."""
    return a == b


def _is_annotation_only(el) -> bool:
    """True if ``el`` is markup that may sit between an anchor run and a following
    ``<w:ins>`` without being visible content: a comment/bookmark range marker, or a
    ``<w:r>`` that carries only a ``<w:commentReference>`` (no text). Such elements
    must be skipped by the insert-idempotency adjacency scan."""
    if el.tag in _ANNOTATION_MARKERS:
        return True
    if el.tag == _W_R:
        has_text = any(c.tag in _TEXT_BEARING for c in el)
        has_ref = el.find(_W_COMMENT_REF) is not None
        return has_ref and not has_text
    return False


def _next_meaningful_sibling(el):
    """The next sibling of ``el`` that is not annotation-only markup (see
    :func:`_is_annotation_only`), or ``None``."""
    sib = el.getnext()
    while sib is not None and _is_annotation_only(sib):
        sib = sib.getnext()
    return sib


def _is_range_closer(el) -> bool:
    """True if ``el`` closes an annotation range that may bracket the anchor — a
    ``<w:commentRangeEnd>`` / ``<w:bookmarkEnd>`` marker, or a comment-reference-only
    run. An insertion steps past these so the new ``<w:ins>`` lands OUTSIDE an
    existing comment range rather than silently extending it. (Range *openers* like
    ``commentRangeStart`` are NOT closers — stepping past one would wrongly enter the
    next range.)"""
    if el.tag in (_W_COMMENT_RANGE_END, _W_BOOKMARK_END):
        return True
    if el.tag == _W_R:
        has_text = any(c.tag in _TEXT_BEARING for c in el)
        has_ref = el.find(_W_COMMENT_REF) is not None
        return has_ref and not has_text
    return False


def _canonical_payload(text: str) -> str:
    """Canonicalise a PROPOSED payload's line breaks: CRLF / lone CR → a single LF.
    Word renders ``"\\r\\n"`` and ``"\\n"`` identically (one ``<w:br/>``), so they are
    the same edit. Canonicalising at the API boundary keeps the WRITTEN revision and
    the EXACT-match idempotency comparison in agreement — otherwise a re-run with
    ``"A\\r\\nB"`` would not match a stored ``"A\\nB"`` and would duplicate the
    insertion (or miss a replace pair and then mis-strike on reload)."""
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _append_text_content(el, text: str) -> None:
    """Append ``text`` to ``el`` as Word run content, tokenizing the control
    characters Word will not render from a literal ``<w:t>``: ``\\t`` →
    ``<w:tab/>``, ``\\n`` → ``<w:br/>`` (CRLF/CR are folded to LF first via
    :func:`_canonical_payload`), and every other maximal stretch of characters → a
    ``<w:t xml:space="preserve">``. Mirrors python-docx's own run-text appender and
    :func:`_run_text`'s reader, so a split fragment or an inserted payload keeps its
    tabs/breaks as real elements (round-tripping cleanly) instead of dead literal
    characters Word ignores."""
    buf: list[str] = []

    def flush() -> None:
        if buf:
            t = OxmlElement("w:t")
            t.set(_XML_SPACE, "preserve")
            t.text = "".join(buf)
            el.append(t)
            buf.clear()

    for ch in _canonical_payload(text):
        if ch == "\t":
            flush()
            el.append(OxmlElement("w:tab"))
        elif ch == "\n":
            flush()
            el.append(OxmlElement("w:br"))
        else:
            buf.append(ch)
    flush()


def _set_run_text(r, text: str) -> None:
    """Replace ``r``'s text-bearing children with freshly tokenized run content
    for ``text`` (see :func:`_append_text_content` — tabs/breaks survive as
    elements). ``<w:rPr>`` is left in place and stays first."""
    for child in list(r):
        if child.tag in _TEXT_BEARING:
            r.remove(child)
    _append_text_content(r, text)


def _new_run_like(r):
    """A fresh empty ``<w:r>`` carrying a deep copy of ``r``'s ``<w:rPr>`` (so the
    new run inherits the original run's character formatting)."""
    nr = OxmlElement("w:r")
    rpr = r.find(_W_RPR)
    if rpr is not None:
        nr.append(copy.deepcopy(rpr))
    return nr


def _split_text_run(r, offset: int):
    """Split ``<w:r>`` element ``r`` at ``offset`` (in ``_run_text`` coordinates).

    Returns ``(left, right)``: ``left`` keeps ``text[:offset]`` (the original
    element, mutated in place), ``right`` is a new sibling inserted immediately
    after holding ``text[offset:]``. ``offset <= 0`` → ``(None, r)`` (nothing to
    the left); ``offset >= len`` → ``(r, None)`` (nothing to the right).
    """
    text = _run_text(r)
    if offset <= 0:
        return None, r
    if offset >= len(text):
        return r, None
    _set_run_text(r, text[:offset])
    right = _new_run_like(r)
    _set_run_text(right, text[offset:])
    r.addnext(right)
    return r, right


def _to_del_text(r) -> None:
    """Rename every ``<w:t>`` child of ``r`` to ``<w:delText>`` (Word stores the
    text of a deleted run in ``delText``, never ``t``)."""
    for child in list(r):
        if child.tag == _W_T:
            dt = OxmlElement("w:delText")
            dt.set(_XML_SPACE, "preserve")
            dt.text = child.text
            r.replace(child, dt)


# --------------------------------------------------------------------------- #
# Match bookkeeping.
# --------------------------------------------------------------------------- #
class _RunSpan(NamedTuple):
    start: int  # inclusive offset of this run in the paragraph's visible text
    end: int  # exclusive offset
    el: object  # the raw lxml <w:r> element
    editable: bool  # True for a plain <w:r> child of <w:p> (splittable/wrappable);
    #                 False for a run nested inside a pre-existing <w:ins> (an
    #                 overlap an edit must refuse rather than cut into).


class _Match(NamedTuple):
    paragraph: object  # docx Paragraph
    spans: list  # list[_RunSpan]
    start: int  # original char offset (inclusive) of the matched text
    end: int  # original char offset (exclusive)
    fi: int  # index into spans of the run containing `start`
    li: int  # index into spans of the run containing `end - 1`


class TrackedDoc:
    """A ``.docx`` accumulating Word-native tracked changes. See module docstring.

    Parameters
    ----------
    base_docx_path:
        Path to the source ``.docx`` to mark up. Loaded once; the original file is
        never mutated — call :meth:`save` to write the marked-up copy.
    now:
        Optional fixed timestamp for the ``w:date`` attribute on every revision
        (defaults to the current UTC time). Pinning it keeps tests deterministic.
    """

    def __init__(self, base_docx_path: Path, *, now: datetime | None = None) -> None:
        self._path = Path(base_docx_path)
        self._doc = self._load(self._path)
        ts = now or datetime.now(timezone.utc)
        # An *aware* timestamp in a non-UTC zone must be converted to UTC before we
        # stamp a trailing "Z" (which asserts UTC) — else the recorded instant is
        # silently wrong. A naive datetime is left as-is (documented "assume UTC").
        if ts.tzinfo is not None:
            ts = ts.astimezone(timezone.utc)
        self._timestamp = ts.strftime("%Y-%m-%dT%H:%M:%SZ")
        self._next_rev_id = self._seed_rev_id()

    @staticmethod
    def _load(path: Path):
        """Load ``path`` into a ``python-docx`` document behind zip-bomb / corrupt
        -archive guards. A ``.docx`` is a zip; we inspect the central directory only
        (no member is decompressed) and reject an archive whose member count or total
        declared uncompressed size is pathological, then hand off to ``python-docx`` —
        wrapping any load failure as :class:`DocxLoadError` so callers get one typed
        error instead of a grab-bag of zip/OPC tracebacks. (No per-member ratio cap:
        legitimate OOXML compresses arbitrarily well; see the constants above.)
        """
        if not is_zipfile(str(path)):
            raise DocxLoadError(f"not a valid .docx (not a zip archive): {path}")
        try:
            with ZipFile(str(path)) as zf:
                infos = zf.infolist()
                if len(infos) > _MAX_ZIP_MEMBERS:
                    raise DocxLoadError(
                        f"refusing .docx with {len(infos)} zip members "
                        f"(cap {_MAX_ZIP_MEMBERS}): {path}")
                total = 0
                for info in infos:
                    total += info.file_size
                    if total > _MAX_ZIP_TOTAL_BYTES:
                        raise DocxLoadError(
                            f"refusing .docx whose uncompressed size exceeds "
                            f"{_MAX_ZIP_TOTAL_BYTES} bytes: {path}")
        except BadZipFile as exc:
            raise DocxLoadError(f"corrupt .docx zip archive: {path}") from exc
        try:
            return Document(str(path))
        except DocxLoadError:
            raise
        except Exception as exc:  # noqa: BLE001 — normalise OPC/zip/parse errors
            raise DocxLoadError(f"failed to load .docx {path}: {exc}") from exc

    # ----------------------------------------------------------------- public
    def insert(self, at_anchor: str, text: str, author: str = "ANTON") -> None:
        """Insert ``text`` as a tracked insertion immediately AFTER ``at_anchor``.

        Always targets the FIRST occurrence of ``at_anchor``. Idempotent: if a
        ``<w:ins>`` by ``author`` with the EXACT same text already follows that
        occurrence, this is a no-op (the proposed text is compared verbatim — a
        differently-spaced insertion is a different edit). Raises
        :class:`AnchorNotFound` if the anchor is not present in the visible text.
        """
        if not text:
            return
        # Canonicalise line breaks ONCE so the written run and the EXACT-match
        # idempotency check below compare the same string (CRLF → LF; see
        # :func:`_canonical_payload`).
        text = _canonical_payload(text)
        match = self._locate(at_anchor)
        if match is None:
            raise AnchorNotFound(f"insert anchor not found: {at_anchor!r}")
        if self._match_touches_protected(match):
            raise RevisionOverlap(
                "insert anchor overlaps protected content — a pre-existing tracked "
                f"insertion or an inline object/field: {at_anchor!r}")

        anchor_run_el = match.spans[match.li].el
        # Anchor-scoped idempotency: the anchor stays visible after an insert, so on a
        # re-run the split from the first call makes the anchor end on a run boundary
        # and our <w:ins> sits in the cluster of insertions that immediately follow it.
        # Scan the WHOLE cluster (multiple distinct payloads may be stacked at one
        # anchor; later ones land in front), skipping annotation-only markup, and
        # compare the proposed text EXACTLY (payload, not whitespace-collapsed).
        if match.end == match.spans[match.li].end:
            if self._find_existing_insertion(anchor_run_el, author, text) is not None:
                return

        # Land the insertion exactly at the anchor end (split if mid-run).
        end_off = match.end - match.spans[match.li].start
        left, _ = _split_text_run(anchor_run_el, end_off)
        anchor_end = left if left is not None else anchor_run_el

        # If the anchor already sits inside a comment range, step past that range's
        # trailing closers (commentRangeEnd / commentReference run / bookmarkEnd) so
        # the new <w:ins> lands OUTSIDE the comment rather than extending its range.
        insert_after = anchor_end
        nxt = insert_after.getnext()
        while nxt is not None and _is_range_closer(nxt):
            insert_after = nxt
            nxt = insert_after.getnext()

        ins_el = self._make_revision("w:ins", author)
        ins_el.append(self._make_text_run(text, rpr_from=anchor_run_el))
        insert_after.addnext(ins_el)

    def delete(self, anchor_text: str, author: str = "ANTON") -> None:
        """Mark ``anchor_text`` (first occurrence) as a tracked deletion
        (``<w:del>`` + ``<w:delText>``).

        Idempotent: checked BEFORE mutating — if this author already has a
        ``<w:del>`` of this text anywhere, it is a no-op, so a re-run never walks on
        to a *second* identical occurrence and compounds the redline. (To delete a
        specific one of several identical phrases, the caller disambiguates upstream;
        ``delete`` itself takes no context and always means "ensure this text is
        struck".) Raises :class:`AnchorNotFound` only when the text is genuinely
        absent (neither visible nor already deleted by ``author``).
        """
        if self._find_existing_del(author, anchor_text) is not None:
            return  # already struck by this author — idempotent
        match = self._locate(anchor_text)
        if match is None:
            raise AnchorNotFound(f"delete anchor not found: {anchor_text!r}")
        matched = self._isolate(match, require_contiguous=True)
        self._wrap_in_del(matched, author)

    def replace(self, find: str, replace: str, author: str = "ANTON",
                before_context: str | None = None,
                after_context: str | None = None) -> None:
        """Replace ``find`` with ``replace`` as a paired tracked change: a
        ``<w:del>`` of ``find`` immediately followed by a ``<w:ins>`` of
        ``replace`` at the same anchor.

        ``before_context`` / ``after_context`` disambiguate which occurrence of
        ``find`` to target when it appears multiple times. Idempotent and
        context-aware (checked BEFORE mutating): a ``del(find)``+``ins(replace)``
        pair by ``author`` whose surrounding visible text matches the SAME context
        is a no-op — so a re-run does not compound, yet two genuinely different
        occurrences (distinct ``before``/``after_context``) each still get their own
        redline. Raises :class:`AnchorNotFound` if ``find`` is neither visible at the
        requested context nor already replaced there, and :class:`RevisionOverlap` if
        the target overlaps a pre-existing revision / foreign markup.

        An empty ``replace`` is a degenerate case — a pure tracked deletion: the
        ``<w:del>`` is emitted with NO trailing (empty) ``<w:ins>``, and idempotency
        keys on a standalone author ``<w:del>`` of ``find`` at this context.
        """
        # Canonicalise the replacement's line breaks ONCE (CRLF → LF) so the written
        # <w:ins> and the EXACT-match idempotency check agree — see
        # :func:`_canonical_payload`. ``find`` is an anchor (whitespace-tolerant), so
        # it needs no canonicalisation (``_collapse_ws`` already folds all breaks).
        replace = _canonical_payload(replace)
        if replace == "":
            # Degenerate replace → a pure tracked deletion. The old code emitted a
            # <w:ins> holding the empty payload, littering the doc with empty
            # insertions that also broke reload-idempotency. Strike only; key
            # idempotency on a *standalone* author <w:del> of `find` at this context.
            if self._find_existing_deletion(author, find,
                                            before_context, after_context) is not None:
                return
            match = self._locate(find, before_context, after_context)
            if match is None:
                raise AnchorNotFound(f"replace target not found: {find!r}")
            self._wrap_in_del(self._isolate(match, require_contiguous=True), author)
            return

        if self._find_existing_replace(author, find, replace,
                                       before_context, after_context) is not None:
            return  # already replaced at this context — idempotent
        match = self._locate(find, before_context, after_context)
        if match is None:
            raise AnchorNotFound(f"replace target not found: {find!r}")
        matched = self._isolate(match, require_contiguous=True)
        del_el = self._wrap_in_del(matched, author)
        ins_el = self._make_revision("w:ins", author)
        ins_el.append(self._make_text_run(replace, rpr_from=matched[0]))
        del_el.addnext(ins_el)

    def comment(self, at_anchor: str, body: str, author: str = "ANTON") -> None:
        """Attach a threaded comment to ``at_anchor`` without changing visible text.

        Materialises ``word/comments.xml`` (via ``python-docx``) and brackets the
        anchor with ``<w:commentRangeStart>`` / ``<w:commentRangeEnd>`` +
        ``<w:commentReference>``. Locates the anchor FIRST, so a genuinely absent
        anchor raises :class:`AnchorNotFound` rather than silently no-op'ing.
        Idempotent SCOPED to the matched RANGE: if the same author already commented
        this exact anchor range with the same ``body`` it is skipped — but the same
        body on a DIFFERENT anchor (even in the same paragraph) is still added.
        """
        match = self._locate(at_anchor)
        if match is None:
            raise AnchorNotFound(f"comment anchor not found: {at_anchor!r}")
        matched = self._isolate(match)
        if self._range_already_commented(matched, author, body):
            return  # this exact range already carries the comment — idempotent
        para = match.paragraph
        # Re-derive python-docx Run wrappers for the (now isolated) matched runs so
        # add_comment gets proper objects bound to the document part.
        by_el = {id(r._r): r for r in para.runs}
        first = by_el[id(matched[0])]
        last = by_el[id(matched[-1])]
        self._doc.add_comment([first, last], text=body, author=author,
                              initials=_initials(author))

    def save(self, out_path: Path) -> None:
        """Write the marked-up document to ``out_path`` (parent dirs created)."""
        out_path = Path(out_path)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        self._doc.save(str(out_path))

    # --------------------------------------------------------------- internals
    def _iter_paragraphs(self, container=None) -> Iterator[object]:
        """Yield every paragraph in true DOCUMENT order, descending into tables
        (including nested tables) so anchors inside table cells are reachable.

        Uses ``iter_inner_content`` so an interleaved ``<w:p>`` / ``<w:tbl>`` /
        ``<w:p>`` sequence is walked in the order it appears (the old code yielded
        *all* body paragraphs before *any* table, so "first occurrence" could land
        in a later paragraph that physically follows a table). Merged table cells
        are de-duplicated by their underlying ``<w:tc>`` element — python-docx's
        grid view repeats the same cell across every column/row it spans, which
        would otherwise yield a merged cell's paragraphs more than once.
        """
        container = container if container is not None else self._doc
        for block in container.iter_inner_content():
            if isinstance(block, Paragraph):
                yield block
            elif isinstance(block, Table):
                seen: set[int] = set()
                for row in block.rows:
                    for cell in row.cells:
                        key = id(cell._tc)
                        if key in seen:
                            continue
                        seen.add(key)
                        yield from self._iter_paragraphs(cell)

    def _locate(self, needle: str, before: str | None = None,
                after: str | None = None) -> _Match | None:
        """Find the first occurrence of ``needle`` (whitespace-tolerant) in the
        VISIBLE text of any paragraph, honouring optional context anchors.

        "Visible" is the *accept-all-insertions* view: plain ``<w:r>`` children of
        the paragraph PLUS runs nested inside a pre-existing ``<w:ins>`` (proposed
        text the reader still sees), but NOT runs inside ``<w:del>`` (struck text
        that vanishes on accept). Including ``<w:ins>`` runs is what stops an anchor
        from silently matching ACROSS an earlier insertion, and lets
        ``before``/``after_context`` resolve against inserted text. Each ``<w:ins>``
        run's span is flagged non-editable; an edit whose matched range touches one
        is refused (:class:`RevisionOverlap`) rather than cut into another revision.
        Idempotency is unaffected — an applied insertion leaves its anchor a plain
        run, which is still matched here.
        """
        nn = _collapse_ws(needle)
        if not nn:
            return None
        nb = _collapse_ws(before) if before else ""
        na = _collapse_ws(after) if after else ""

        for para in self._iter_paragraphs():
            spans, full = self._visible_spans(para)
            if not spans:
                continue
            norm, starts, ends = _normalize_with_map(full)

            search_from = 0
            while True:
                ns = norm.find(nn, search_from)
                if ns < 0:
                    break
                ne = ns + len(nn)
                if self._context_ok(norm, ns, ne, nb, na):
                    o_start = starts[ns]
                    o_end = ends[ne - 1]
                    fi = self._run_index(spans, o_start, end=False)
                    li = self._run_index(spans, o_end, end=True)
                    return _Match(para, spans, o_start, o_end, fi, li)
                search_from = ns + 1
        return None

    @staticmethod
    def _context_ok(norm: str, ns: int, ne: int, nb: str, na: str) -> bool:
        if nb and not norm[:ns].rstrip().endswith(nb):
            return False
        if na and not norm[ne:].lstrip().startswith(na):
            return False
        return True

    @staticmethod
    def _run_index(spans: list, offset: int, *, end: bool) -> int:
        """Index of the run containing ``offset`` (for ``end=True`` the run
        containing ``offset - 1``, i.e. the last matched character)."""
        target = offset - 1 if end else offset
        for i, span in enumerate(spans):
            if span.start <= target < span.end:
                return i
        return len(spans) - 1  # defensive: clamp to last run

    @staticmethod
    def _visible_spans(para) -> tuple[list, str]:
        """Build the ordered run-token stream for ``para``'s visible text plus its
        concatenated string. Walks the paragraph's direct children in order: a plain
        ``<w:r>`` → an editable span; each ``<w:r>`` nested inside a ``<w:ins>`` → a
        non-editable span (visible, but owned by a pre-existing revision). ``<w:del>``
        is skipped (struck text); so are ``<w:hyperlink>`` / ``<w:sdt>`` / other
        wrappers (not surfaced by python-docx ``Paragraph.runs`` — documented
        boundary) and empty/non-text runs. ``_run_text`` (not ``Run.text``) supplies
        span lengths so they match exactly what ``_isolate`` / ``_split_text_run``
        later compute — any divergence would corrupt the split offsets."""
        spans: list[_RunSpan] = []
        parts: list[str] = []
        pos = 0
        for child in para._p:
            if child.tag == _W_R:
                t = _run_text(child)
                if not t:
                    continue
                # A plain run is editable only if it carries no inline object / field
                # mixed in with its text (else splitting/striking would move it).
                editable = not _run_has_foreign_content(child)
                spans.append(_RunSpan(pos, pos + len(t), child, editable))
                parts.append(t)
                pos += len(t)
            elif child.tag == _W_INS:
                for r in child:
                    if r.tag != _W_R:
                        continue
                    t = _run_text(r)
                    if not t:
                        continue
                    spans.append(_RunSpan(pos, pos + len(t), r, False))
                    parts.append(t)
                    pos += len(t)
            # <w:del> → struck text (excluded). <w:hyperlink>/<w:sdt>/other → not
            # surfaced, preserving the documented matching boundaries.
        return spans, "".join(parts)

    @staticmethod
    def _match_touches_protected(match: _Match) -> bool:
        """True if any run in the matched range ``[fi, li]`` is a non-editable span —
        either nested inside a pre-existing ``<w:ins>`` (cutting into another
        revision) or carrying foreign inline content such as a drawing / field
        (see :func:`_run_has_foreign_content`). Either way the edit cannot apply
        cleanly; callers raise :class:`RevisionOverlap`."""
        return any(not match.spans[k].editable
                   for k in range(match.fi, match.li + 1))

    def _isolate(self, match: _Match, *, require_contiguous: bool = False) -> list:
        """Split boundary runs so the matched text occupies a contiguous run of
        whole ``<w:r>`` elements; return those elements in order. Refuses
        (:class:`RevisionOverlap`) if the matched range overlaps a pre-existing
        ``<w:ins>`` — those runs are owned by another revision and must not be
        split or swept into a new one.

        ``require_contiguous`` (the strike path: ``delete`` / ``replace``) ALSO
        verifies, BEFORE any run is split, that the matched source runs are
        consecutive siblings with no foreign markup between them (a ``<w:del>``,
        comment / bookmark marker, inline object or hyperlink). Doing this check
        pre-split means a refused strike leaves the document completely unmutated —
        no orphaned splits from an operation that did not apply. ``comment`` leaves
        it ``False``: a comment range may legitimately bracket such markers."""
        if self._match_touches_protected(match):
            raise RevisionOverlap(
                "anchor overlaps protected content (a pre-existing tracked insertion, "
                "or an inline object/field embedded in the run); narrow the anchor to "
                "plain text or apply this edit before the insertion")
        spans = match.spans
        fi, li = match.fi, match.li
        if require_contiguous:
            for k in range(fi, li):
                if spans[k].el.getnext() is not spans[k + 1].el:
                    raise RevisionOverlap(
                        "cannot strike a span broken by intervening markup (a "
                        "comment / bookmark / revision marker, inline object, or "
                        "hyperlink sits between the matched runs); narrow the anchor, "
                        "or delete before adding comments/insertions over this span")
        if fi == li:
            r = spans[fi].el
            base = spans[fi].start
            left, _after = _split_text_run(r, match.end - base)
            _before, matched = _split_text_run(left, match.start - base)
            return [matched]

        rf = spans[fi].el
        _before, matched_first = _split_text_run(rf, match.start - spans[fi].start)
        rl = spans[li].el
        matched_last, _after = _split_text_run(rl, match.end - spans[li].start)
        middle = [spans[k].el for k in range(fi + 1, li)]
        return [matched_first] + middle + [matched_last]

    def _wrap_in_del(self, matched: list, author: str):
        """Wrap the matched run elements in a ``<w:del>`` and convert their ``<w:t>``
        to ``<w:delText>``. Returns the ``<w:del>`` element.

        The matched runs MUST be CONTIGUOUS siblings — wrapping a span broken by a
        comment / bookmark marker, another revision, an inline drawing or a
        hyperlink would orphan that markup and reorder it past the deletion (a
        half-struck comment, an insertion that hops to the wrong side). The strike
        callers establish this invariant pre-split via
        :meth:`_isolate` (``require_contiguous=True``), so the document is never
        mutated by a refused op; this re-check is a cheap defensive backstop that
        raises :class:`RevisionOverlap` should any future caller pass a
        non-contiguous run set. (``<w:ins>`` overlaps are caught in :meth:`_isolate`
        too; this covers the text-less ``<w:del>`` / comment / bookmark / inline /
        hyperlink siblings.)"""
        for prev, nxt in zip(matched, matched[1:]):
            if prev.getnext() is not nxt:
                raise RevisionOverlap(
                    "cannot strike a span broken by intervening markup (a comment / "
                    "bookmark / revision marker, inline object, or hyperlink sits "
                    "between the matched runs); narrow the anchor, or delete before "
                    "adding comments/insertions over this span")
        del_el = self._make_revision("w:del", author)
        matched[0].addprevious(del_el)
        for el in matched:
            _to_del_text(el)
            del_el.append(el)
        return del_el

    def _make_revision(self, tag: str, author: str):
        """A ``<w:ins>`` / ``<w:del>`` element with a unique id, author and date."""
        el = OxmlElement(tag)
        el.set(_W_ID, str(self._take_rev_id()))
        el.set(_W_AUTHOR, author)
        el.set(_W_DATE, self._timestamp)
        return el

    @staticmethod
    def _make_text_run(text: str, *, rpr_from=None):
        """A plain ``<w:r>`` holding ``text``, inheriting ``rpr_from``'s formatting."""
        r = OxmlElement("w:r")
        if rpr_from is not None:
            rpr = rpr_from.find(_W_RPR)
            if rpr is not None:
                r.append(copy.deepcopy(rpr))
        _set_run_text(r, text)
        return r

    # -- idempotency scanning -------------------------------------------------
    @staticmethod
    def _is_revision(el, tag: str, author: str) -> bool:
        """True if ``el`` exists, is a ``<w:ins>``/``<w:del>`` (``tag``) and is
        attributed to ``author``. Text equality is checked separately by the caller
        (anchor text → whitespace-tolerant; proposed payload → exact)."""
        return el is not None and el.tag == tag and el.get(_W_AUTHOR) == author

    def _find_existing_insertion(self, anchor_run_el, author: str, text: str):
        """Scan the cluster of insertions that immediately follow ``anchor_run_el`` and
        return the ``<w:ins>`` by ``author`` whose payload EXACTLY equals ``text``, else
        ``None``. Crosses other ``<w:ins>`` in the cluster (distinct payloads stacked at
        one anchor) and annotation-only markup; stops at the first real content. Without
        this an insert re-run only checked the single adjacent ins and would duplicate a
        payload that sits behind a different one."""
        sib = anchor_run_el.getnext()
        while sib is not None:
            if sib.tag == _W_INS:
                if sib.get(_W_AUTHOR) == author and _payload_eq(_element_text(sib), text):
                    return sib
                sib = sib.getnext()        # different insertion in the cluster — skip
                continue
            if _is_annotation_only(sib):
                sib = sib.getnext()
                continue
            break                          # real content — the cluster ends here
        return None

    def _find_existing_del(self, author: str, find: str):
        """First ``<w:del>`` by ``author`` whose text anchor-matches ``find``
        (whitespace-tolerant), anywhere in the body; else ``None``."""
        for el in self._doc.element.body.iter(_W_DEL):
            if el.get(_W_AUTHOR) == author and _anchor_eq(_element_text(el), find):
                return el
        return None

    def _find_existing_replace(self, author: str, find: str, replace: str,
                               before: str | None, after: str | None):
        """First applied ``del(find)``→``ins(replace)`` pair by ``author`` whose
        surrounding visible text also satisfies the supplied ``before``/``after``
        context; else ``None``. Anchors (``find``) match whitespace-tolerantly; the
        proposed ``replace`` payload must match EXACTLY."""
        nb = _collapse_ws(before) if before else ""
        na = _collapse_ws(after) if after else ""
        for del_el in self._doc.element.body.iter(_W_DEL):
            if del_el.get(_W_AUTHOR) != author:
                continue
            if not _anchor_eq(_element_text(del_el), find):
                continue
            ins_el = del_el.getnext()
            if not (self._is_revision(ins_el, _W_INS, author)
                    and _payload_eq(_element_text(ins_el), replace)):
                continue
            if (nb or na) and not self._pair_context_ok(del_el, ins_el, nb, na):
                continue
            return del_el
        return None

    @staticmethod
    def _pair_context_ok(del_el, ins_el, nb: str, na: str) -> bool:
        """Check that the visible (normal-run) text immediately before ``del_el``
        ends with ``nb`` and immediately after ``ins_el`` starts with ``na``, within
        their shared paragraph. Mirrors :meth:`_context_ok` so a re-run with the same
        context resolves to the same instance."""
        p = del_el.getparent()
        if p is None:
            return False
        children = list(p)
        try:
            di, ii = children.index(del_el), children.index(ins_el)
        except ValueError:
            return False
        before_text = _visible_run_text(children[:di])
        after_text = _visible_run_text(children[ii + 1:])
        if nb and not _collapse_ws(before_text).endswith(nb):
            return False
        if na and not _collapse_ws(after_text).startswith(na):
            return False
        return True

    def _find_existing_deletion(self, author: str, find: str,
                                before: str | None, after: str | None):
        """First STANDALONE ``<w:del>`` of ``find`` by ``author`` (NOT immediately
        followed by an author ``<w:ins>`` — that pairing is a replace, handled by
        :meth:`_find_existing_replace`) whose surrounding context satisfies
        ``before``/``after``; else ``None``. Backs the idempotency of
        ``replace(find, "")`` (a pure tracked deletion) so a re-run after the text is
        already struck is a no-op rather than an ``AnchorNotFound``."""
        nb = _collapse_ws(before) if before else ""
        na = _collapse_ws(after) if after else ""
        for del_el in self._doc.element.body.iter(_W_DEL):
            if del_el.get(_W_AUTHOR) != author:
                continue
            if not _anchor_eq(_element_text(del_el), find):
                continue
            nxt = del_el.getnext()
            if self._is_revision(nxt, _W_INS, author) and _element_text(nxt) != "":
                # A del followed by a NON-EMPTY author ins is a real replace pair.
                # An EMPTY author ins is the legacy empty-replace shape (older code
                # emitted one) — still a standalone deletion for idempotency.
                continue
            if (nb or na) and not self._del_context_ok(del_el, nb, na):
                continue
            return del_el
        return None

    @staticmethod
    def _del_context_ok(del_el, nb: str, na: str) -> bool:
        """Like :meth:`_pair_context_ok` but for a standalone deletion: the visible
        text immediately before ``del_el`` ends with ``nb`` and the text immediately
        after it starts with ``na``, within their shared paragraph."""
        p = del_el.getparent()
        if p is None:
            return False
        children = list(p)
        try:
            di = children.index(del_el)
        except ValueError:
            return False
        before_text = _visible_run_text(children[:di])
        after_text = _visible_run_text(children[di + 1:])
        if nb and not _collapse_ws(before_text).endswith(nb):
            return False
        if na and not _collapse_ws(after_text).startswith(na):
            return False
        return True

    def _range_already_commented(self, matched: list, author: str, body: str) -> bool:
        """True if the EXACT matched run range is already bracketed by a comment from
        ``author`` with body ``body`` — i.e. a ``<w:commentRangeStart>`` before the
        first matched run and the paired ``<w:commentRangeEnd>`` after the last, whose
        comment matches. Scoping to the range (not the whole paragraph) lets the same
        body be commented on two distinct anchors in one paragraph, while a true
        re-run of the same anchor stays a no-op."""
        first, last = matched[0], matched[-1]
        parent = first.getparent()
        if parent is None:
            return False
        children = list(parent)
        try:
            fi, li = children.index(first), children.index(last)
        except ValueError:
            return False
        for c in self._doc.comments:
            if c.author != author or (c.text or "") != body:
                continue
            cid = str(c.comment_id)
            start_idx = end_idx = None
            for i, ch in enumerate(children):
                if ch.tag == _W_COMMENT_RANGE_START and ch.get(_W_ID) == cid:
                    start_idx = i
                elif ch.tag == _W_COMMENT_RANGE_END and ch.get(_W_ID) == cid:
                    end_idx = i
            # Require the range to bracket EXACTLY this run span — only annotation-only
            # markup may sit between the markers and the matched runs. A larger comment
            # that merely *contains* this range (with other text between) is a distinct
            # anchor and must NOT suppress a new comment.
            if (start_idx is not None and end_idx is not None
                    and start_idx < fi and li < end_idx
                    and all(_is_annotation_only(children[k]) for k in range(start_idx + 1, fi))
                    and all(_is_annotation_only(children[k]) for k in range(li + 1, end_idx))):
                return True
        return False

    def _seed_rev_id(self) -> int:
        """Next free revision id = 1 + max existing ``w:id`` on any ins/del."""
        max_id = 0
        body = self._doc.element.body
        for tag in (_W_INS, _W_DEL):
            for el in body.iter(tag):
                raw = el.get(_W_ID)
                if raw and raw.isdigit():
                    max_id = max(max_id, int(raw))
        return max_id + 1

    def _take_rev_id(self) -> int:
        rid = self._next_rev_id
        self._next_rev_id += 1
        return rid


def _initials(author: str) -> str:
    """Cheap initials for the comment ``w:initials`` attribute ("ANTON" → "A",
    "Jane Smith" → "JS")."""
    parts = [p for p in re.split(r"\s+", author.strip()) if p]
    if not parts:
        return ""
    if len(parts) == 1:
        return parts[0][0].upper()
    return "".join(p[0].upper() for p in parts)
