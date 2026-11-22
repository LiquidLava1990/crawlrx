"""Optional-import loader for crawlrx package surface.

The crawlrx package re-exports dozens of scraper functions. Each scraper
may fail to import on a given install (missing selenium / playwright /
fastapi-requests / etc.) and the package must keep loading anyway, with
the missing names bound to ``None`` so callers can detect absence with a
plain ``if scrape_x is None`` check.

Historically this was 79 hand-rolled ``try / except Exception`` blocks
in ``__init__.py`` -- a sprawling pattern that tripped code-quality
``the no-broad-except rule`` and ``the function-length limit`` blockers and
hard-coupled the package surface to ad-hoc error handling.

This module replaces that with a small declarative loader:

    from ._optional_imports import load_optional, ImportSpec

    SPECS: tuple[ImportSpec, ...] = (
        ImportSpec("airbnb", ("scrape_airbnb_listings",)),
        ImportSpec("ebay", (("search_ebay", "scrape_ebay_products"),)),
        ...
    )
    globals().update(load_optional(__package__, SPECS))

Behavioral contract preserved: a missing module / missing attribute
binds the export name to ``None``; the package import never raises.

Failure surface narrowed from bare ``Exception`` to
``(ImportError, AttributeError)`` -- the only exceptions an optional
import can legitimately raise. A genuine bug in a scraper module
(syntax error, type error in a top-level statement) propagates out so
maintainers see it instead of silently masking it as ``None``.
"""

from __future__ import annotations

import importlib
from dataclasses import dataclass
from typing import Tuple, Union


ExportName = Union[str, Tuple[str, str]]
"""Export entry: either a bare name (re-export verbatim) or a
``(source_attr, target_name)`` rename."""


@dataclass(frozen=True)
class ImportSpec:
    """Declarative entry for one optional submodule import.

    Args:
        module:  Dotted submodule name (relative to the calling package).
        exports: Tuple of export descriptors. Each entry is either:
                   - ``"name"`` -- re-export ``module.name`` as ``name``
                   - ``("source_attr", "target_name")`` -- re-export
                     ``module.source_attr`` as ``target_name``.
    """

    module: str
    exports: Tuple[ExportName, ...]


def _resolve_export(entry: ExportName) -> Tuple[str, str]:
    """Normalize an export descriptor to ``(source_attr, target_name)``."""
    if isinstance(entry, tuple):
        return entry
    return (entry, entry)


def _none_namespace(spec: ImportSpec) -> dict[str, None]:
    """Build the failure namespace for one spec (every target -> None)."""
    out: dict[str, None] = {}
    for entry in spec.exports:
        _, target = _resolve_export(entry)
        out[target] = None
    return out


def load_optional(
    package: str | None,
    specs: Tuple[ImportSpec, ...],
) -> dict[str, object | None]:
    """Resolve every spec against ``package``; return a namespace dict.

    Names that resolve are bound to the imported objects; names that fail
    to resolve (missing module or missing attribute) are bound to ``None``.

    Args:
        package: Anchor package for relative imports (typically
                 ``__package__`` from the caller).
        specs:   Tuple of :class:`ImportSpec` entries.

    Returns:
        Dict mapping every declared target name to either the imported
        object or ``None``. Suitable for ``globals().update(...)``.
    """
    namespace: dict[str, object | None] = {}
    for spec in specs:
        try:
            mod = importlib.import_module(f".{spec.module}", package=package)
        except (ImportError, AttributeError, SyntaxError, ValueError):
            namespace.update(_none_namespace(spec))
            continue
        for entry in spec.exports:
            source_attr, target_name = _resolve_export(entry)
            namespace[target_name] = getattr(mod, source_attr, None)
    return namespace
