"""Regression tests for GeeTest vision selections and truthful fallback results."""
import unittest
from unittest.mock import AsyncMock, patch

from crawlrx.solver_service.solvers._geetest_opencv import solve_with_opencv
from crawlrx.solver_service.solvers._geetest_strategies import _detect_target_icons
from crawlrx.solver_service.solvers.geetest_solver import GeeTestSolver


class GeeTestVisionTests(unittest.IsolatedAsyncioTestCase):
    async def test_unverified_opencv_estimate_is_not_a_token(self):
        result = await solve_with_opencv('fixture', 'fixture')
        self.assertFalse(result.success)
        self.assertIsNone(result.token)

    async def test_valid_json_and_fenced_json_preserve_order(self):
        for answer in ('{"indices":[2,0]}', '```json\n{"indices":[2,0]}\n```'):
            with self.subTest(answer=answer), patch('crawlrx.solver_service.core.vision_router.analyze_bytes', AsyncMock(return_value=answer)):
                self.assertEqual(await _detect_target_icons(b'fixture', 'select icons', 3), [2, 0])

    async def test_ambiguous_or_invalid_selection_is_rejected(self):
        for answer in (None, 'no idea', '{"indices":[]}', '{"indices":[0,0]}',
                       '{"indices":[-1]}', '{"indices":[3]}', '{"indices":[true]}',
                       'prefix {"indices":[1]}'):
            with self.subTest(answer=answer), patch('crawlrx.solver_service.core.vision_router.analyze_bytes', AsyncMock(return_value=answer)):
                self.assertEqual(await _detect_target_icons(b'fixture', 'select icons', 3), [])

    async def test_unreachable_browser_does_not_report_ready(self):
        with patch.dict('os.environ', {'GEETEST_CDP_PORT': '1'}):
            result = GeeTestSolver({}).health_check()
        self.assertFalse(result['healthy'])
        self.assertFalse(result['details']['cdp_available'])

    async def test_invalid_cdp_response_does_not_report_ready(self):
        opener = unittest.mock.MagicMock()
        opener.open.return_value.__enter__.return_value.read.return_value = b'{}'
        with patch('urllib.request.build_opener', return_value=opener):
            result = GeeTestSolver({}).health_check()
        self.assertFalse(result['healthy'])


if __name__ == '__main__':
    unittest.main()
