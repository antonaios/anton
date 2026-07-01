"""Template registry — loads `templates/templates.yaml` and resolves template
names to a TemplateSpec describing path, cell map, version, and hash.

Hashing policy: the registry computes a sha256 over the .xlsx file at load
time and stores it on the TemplateSpec. Each `run` re-hashes the live file
and compares; mismatch raises TemplateHashMismatch. This catches the case
where a template was hand-edited but the cell map wasn't re-validated.
"""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import yaml

from valuation.exceptions import TemplateNotFound
from valuation.models import TemplateSpec


class TemplateRegistry:
    """Loads templates.yaml and exposes resolution by name.

    Usage:
        registry = TemplateRegistry.from_yaml(Path("templates/templates.yaml"))
        spec = registry.resolve("lbo")
    """

    def __init__(self, specs: dict[str, TemplateSpec]) -> None:
        self._specs = specs

    # ------------------------------------------------------------------ load

    @classmethod
    def from_yaml(cls, yaml_path: Path) -> "TemplateRegistry":
        """Load registry from YAML config. Hashes each template file at load
        time so subsequent edits are detected on the next run.

        Raises:
            FileNotFoundError: if yaml_path doesn't exist.
            TemplateNotFound: if a template's `path` doesn't resolve on disk.
            ValueError: on schema violations (missing required fields).
        """
        if not yaml_path.exists():
            raise FileNotFoundError(f"Template registry not found: {yaml_path}")

        raw = yaml.safe_load(yaml_path.read_text(encoding="utf-8")) or {}
        templates = raw.get("templates") or {}
        if not isinstance(templates, dict):
            raise ValueError(f"templates.yaml: 'templates' must be a mapping (got {type(templates).__name__})")

        specs: dict[str, TemplateSpec] = {}
        for name, entry in templates.items():
            if not isinstance(entry, dict):
                raise ValueError(f"templates.yaml: entry for {name!r} must be a mapping")
            specs[name] = cls._build_spec(name, entry, yaml_path.parent)
        return cls(specs)

    @staticmethod
    def _build_spec(name: str, entry: dict[str, Any], base_dir: Path | None = None) -> TemplateSpec:
        # Required fields
        for required in ("path", "version", "inputs", "outputs"):
            if required not in entry:
                raise ValueError(f"templates.yaml: {name!r} missing required field {required!r}")

        # A relative ``path`` resolves against the registry file's directory, so a
        # repo-bundled template works regardless of CWD; an absolute path (e.g. an
        # operator's own ``X:/…`` template) is used unchanged.
        path = Path(entry["path"])
        if not path.is_absolute() and base_dir is not None:
            path = (base_dir / path).resolve()
        if not path.exists():
            raise TemplateNotFound(
                f"templates.yaml: template {name!r} declared at {path} but file does not exist"
            )

        inputs = entry["inputs"] or {}
        outputs = entry["outputs"] or {}
        optional_inputs = entry.get("optional_inputs") or {}
        validation = entry.get("validation") or []
        post_recalc = entry.get("post_recalc_hardcode") or []

        # Normalise post_recalc_hardcode shape
        normalised_post: list[dict[str, Any]] = []
        for step in post_recalc:
            if not isinstance(step, dict):
                raise ValueError(f"{name}: post_recalc_hardcode entries must be mappings")
            if "source" not in step or "targets" not in step:
                raise ValueError(f"{name}: post_recalc_hardcode requires 'source' and 'targets'")
            targets = step["targets"]
            if isinstance(targets, str):
                targets = [targets]
            normalised_post.append({
                "source": step["source"],
                "targets": list(targets),
                "converge": bool(step.get("converge", False)),
                "tolerance": float(step.get("tolerance", 1e-4)),
                "max_iters": int(step.get("max_iters", 10)),
            })

        return TemplateSpec(
            name=name,
            path=path,
            version=str(entry["version"]),
            description=entry.get("description", ""),
            inputs=dict(inputs),
            outputs=dict(outputs),
            optional_inputs=dict(optional_inputs),
            validation=list(validation),
            post_recalc_hardcode=normalised_post,
            template_hash=TemplateRegistry._hash_file(path),
        )

    # ---------------------------------------------------------------- resolve

    def resolve(self, name: str) -> TemplateSpec:
        if name not in self._specs:
            raise TemplateNotFound(
                f"Template {name!r} not registered. Available: {sorted(self._specs)}"
            )
        return self._specs[name]

    def names(self) -> list[str]:
        return sorted(self._specs)

    def all(self) -> dict[str, TemplateSpec]:
        return dict(self._specs)

    # ------------------------------------------------------------------ utils

    @staticmethod
    def _hash_file(path: Path) -> str:
        """sha256 of the file contents. Used to detect template edits."""
        h = hashlib.sha256()
        with path.open("rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
        return f"sha256:{h.hexdigest()}"
