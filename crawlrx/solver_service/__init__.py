"""Challenge-solving service subpackage.

Assembled from the canonical local solver stack: the FastAPI app
(``main``), the router + task queue, per-challenge solvers under
``solvers/``, and shared helpers under ``core/``.  Submodules are
imported directly (``from crawlrx.solver_service import main``); this
package ``__init__`` deliberately stays import-light so a missing
optional dependency never blocks importing the subpackage.
"""
