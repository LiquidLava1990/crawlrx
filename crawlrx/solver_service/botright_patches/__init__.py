"""Engine-compat patches for the ``botright`` package, applied at startup.

``apply_all_patches`` imports each sibling patch module and runs its
``apply()``. Individual failures are logged and never propagate: a missing
or half-installed engine must not abort solver registration.
"""
from __future__ import annotations

import importlib
import logging
from typing import Any, Callable, List, Tuple

logger = logging.getLogger(__name__)

# Sibling patch modules, applied in order.
_PATCH_MODULES: Tuple[str, ...] = (".botright", ".page", ".hcaptcha", ".recaptcha_bridge")


def apply_all_patches() -> List[str]:
    """Apply every available patch; return the names that succeeded."""
    applied: List[str] = []
    for module_name in _PATCH_MODULES:
        short_name = module_name.rpartition(".")[2]
        try:
            module = importlib.import_module(module_name, __name__)
            apply_patch: Callable[..., Any] = getattr(module, "apply")
            apply_patch()
            applied.append(short_name)
            logger.debug("Applied botright patch: %s", short_name)
        except Exception as e:  # noqa: BLE001 - patch failure must not abort registration
            logger.warning("Botright patch '%s' not applied: %s", short_name, e)
    return applied


__all__ = ["apply_all_patches"]
