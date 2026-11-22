import tempfile
from pathlib import Path
from unittest.mock import patch
from crawlrx.solver_service.solvers.headlessx_solver import HeadlessXSolver


def test_cookie_directory_uses_environment_and_explicit_config_wins():
    with tempfile.TemporaryDirectory() as root, patch.dict('os.environ', {
        'CRAWLRX_HEADLESSX_COOKIE_DIR': str(Path(root) / 'configured'),
    }):
        solver = HeadlessXSolver({'enabled': True})
        assert solver.cookie_dir == Path(root) / 'configured'
        assert solver.cookie_dir.is_dir()
        explicit = HeadlessXSolver({'enabled': True, 'cookie_dir': str(Path(root) / 'explicit')})
        assert explicit.cookie_dir == Path(root) / 'explicit'
        assert explicit.cookie_dir.is_dir()
