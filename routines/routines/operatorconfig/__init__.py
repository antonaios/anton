"""Operator-config module (#operator-tab).

Read + surgically write the ``_claude/`` dashboard-config file family so
the dashboard's OPERATOR tab can edit operator-level config without
opening Obsidian. The vault file stays the single source of truth: a
write replaces ONLY the relevant YAML payload (fenced block, frontmatter
scalar, or frontmatter list items) and leaves every other byte of the
file — prose, comments, other sections — untouched.

Layout:

* ``blocks.py``       — fenced ``## section`` YAML-block surgery (write-side
                        mirror of ``routines.shared.md_config``)
* ``profile_edit.py`` — comment-preserving frontmatter line surgery for
                        ``profile.md``
* ``models.py``       — pydantic section models (strict PUT validation)
* ``store.py``        — per-section read/write, mtime conflict guard,
                        audit JSONL
"""

from routines.operatorconfig.models import (
    BannersData,
    CoverageData,
    ProfileData,
    SectorsData,
    WatchlistData,
    WRITABLE_SECTIONS,
)
from routines.operatorconfig.store import (
    ConflictError,
    read_config,
    write_section,
)

__all__ = [
    "BannersData",
    "CoverageData",
    "ProfileData",
    "SectorsData",
    "WatchlistData",
    "WRITABLE_SECTIONS",
    "ConflictError",
    "read_config",
    "write_section",
]
