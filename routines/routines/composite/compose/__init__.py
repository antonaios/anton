"""Per-composite-key shaping handlers (#26b).

Importing this package registers every handler in the bundled compose
modules with the ``ComposeRegistry`` in ``routines.api.routes.compose``.
``routines.api.app`` imports this package at startup so the registrations
fire before the first request.

To add a compose key: create ``routines/composite/compose/<key>.py``
following the ``compose_pitch_payload`` pattern, then import it here.
"""

from routines.composite.compose import compose_pitch_payload  # noqa: F401
