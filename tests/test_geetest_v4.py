"""GeeTest v4 compatibility and isolation regression tests."""
import asyncio
import json
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from crawlrx.solver_service import main
from crawlrx.solver_service.models import CaptchaMethod, Task, TaskResult, TaskState
from crawlrx.solver_service.task_queue import TaskQueue
from crawlrx.solver_service.solver_router import SolverRouter
from crawlrx.solver_service.solvers import _geetest_browser, _geetest_strategies
from crawlrx.solver_service.solvers.geetest_solver import GeeTestSolver
from types import SimpleNamespace
import time


class V4ApiTests(unittest.TestCase):
    def setUp(self):
        self.queue = TaskQueue()
        for item in (patch.object(main, 'task_queue', self.queue),
                     patch.object(main, 'verify_api_key', return_value=True)):
            item.start()
            self.addCleanup(item.stop)
        self.client = TestClient(main.app)

    def test_submit_requires_id_and_accepts_v4(self):
        base = {'key': '', 'method': 'geetest_v4', 'pageurl': 'https://example.org', 'json': '1'}
        result = self.client.post('/in.php', data=base).json()
        self.assertEqual(result['status'], 0)
        self.assertIn('ERROR_', result['request'])
        result = self.client.post('/in.php', data={**base, 'captcha_id': 'fixture'}).json()
        self.assertEqual(result['status'], 1)
        task = self.queue._tasks[result['request']]
        self.assertEqual(task.method, CaptchaMethod.GEETEST)
        self.assertEqual(task.params['method_hint'], 'geetest_v4')
        self.assertEqual(task.params['captcha_id'], 'fixture')

    def result(self, answer, expected='fixture'):
        self.queue._tasks['fixture'] = Task(
            id='fixture', method=CaptchaMethod.GEETEST,
            params={'method_hint': 'geetest_v4', 'captcha_id': expected},
            state=TaskState.SOLVED, result=TaskResult(success=True, token=json.dumps(answer)))
        return self.client.get('/res.php?key=&action=get&id=fixture&json=1').json()

    def test_result_requires_complete_matching_bundle(self):
        answer = {'captcha_id': 'fixture', 'lot_number': 'lot', 'pass_token': 'pass',
                  'gen_time': '123', 'captcha_output': 'output'}
        self.assertEqual(self.result(answer), answer)
        for missing in answer:
            with self.subTest(missing=missing):
                partial = {k: v for k, v in answer.items() if k != missing}
                self.assertEqual(self.result(partial)['request'], 'ERROR_CAPTCHA_UNSOLVABLE')
        self.assertEqual(self.result(answer, expected='other')['request'], 'ERROR_CAPTCHA_UNSOLVABLE')


class V4ConcurrencyTests(unittest.IsolatedAsyncioTestCase):
    async def test_unexpected_processor_exception_finishes_task(self):
        queue = TaskQueue()
        task = Task(id='error', method=CaptchaMethod.GEETEST, params={})
        queue._stats['by_type']['geetest'] = {'submitted': 1, 'solved': 0, 'failed': 0}
        async def processor(_task):
            raise NameError('fixture error')
        queue.set_processor(processor)
        await queue._process_task(task)
        self.assertEqual(task.state, TaskState.FAILED)
        self.assertEqual(task.result.error_code, 'ERROR_INTERNAL')
        self.assertNotIn(task.id, queue._processing)

    async def test_solver_serializes_shared_browser_state(self):
        solver = GeeTestSolver({})
        active = 0
        max_active = 0
        async def fake_solve(task):
            nonlocal active, max_active
            active += 1
            max_active = max(max_active, active)
            await asyncio.sleep(0.02)
            active -= 1
            return TaskResult(success=False)
        with patch.object(solver, '_solve_serial', side_effect=fake_solve):
            await asyncio.gather(solver.solve(None), solver.solve(None))
        self.assertEqual(max_active, 1)

    async def test_router_timeout_cancels_solver_without_opencv_fallback(self):
        solver = GeeTestSolver({'timeout': 0.05, 'max_retries': 1})
        task = Task(id='timeout', method=CaptchaMethod.GEETEST, params={
            'method_hint': 'geetest_v4', 'captcha_id': 'fixture', 'pageurl': 'https://example.org'})
        async def stalled(*args):
            await asyncio.Event().wait()
        with patch.object(solver, '_solve_with_browser', side_effect=stalled), \
             patch('crawlrx.solver_service.solvers.geetest_solver.solve_with_opencv') as fallback:
            result = await asyncio.wait_for(
                SolverRouter()._invoke_solver(solver, task, 'geetest', time.time()), timeout=1)
        self.assertEqual(result.error_code, 'ERROR_TIMEOUT')
        fallback.assert_not_called()

    async def test_generic_fallback_cannot_mark_geetest_solved(self):
        router = SolverRouter()
        task = Task(id='generic', method=CaptchaMethod.GEETEST, params={
            'method_hint': 'geetest_v4', 'captcha_id': 'fixture'})
        async def generic(*args, **kwargs):
            return TaskResult(success=True, token='not-a-geetest-response')
        with patch.object(router, '_try_solver', side_effect=generic):
            result = await router._run_chain(task, 'geetest', time.time())
        self.assertFalse(result.success)
        self.assertEqual(result.error_code, 'ERROR_ALL_SOLVERS_FAILED')

    async def test_router_accepts_only_matching_complete_geetest_result(self):
        router = SolverRouter()
        task = Task(id='real', method=CaptchaMethod.GEETEST, params={
            'method_hint': 'geetest_v4', 'captcha_id': 'fixture'})
        answer = {'captcha_id': 'fixture', 'lot_number': 'lot', 'pass_token': 'pass',
                  'gen_time': '123', 'captcha_output': 'output'}
        async def solved(*args, **kwargs):
            return TaskResult(success=True, token=json.dumps(answer))
        with patch.object(router, '_try_solver', side_effect=solved):
            result = await router._run_chain(task, 'geetest', time.time())
        self.assertTrue(result.success)

    async def test_cancellation_is_not_swallowed_during_page_probe_or_cleanup(self):
        async def stalled():
            await asyncio.Event().wait()
        locator = SimpleNamespace(count=stalled)
        page = SimpleNamespace(locator=lambda _: locator, close=stalled)
        host = SimpleNamespace(page=page)
        for action in (_geetest_strategies._probe_once, _geetest_browser._safe_close_page):
            with self.subTest(action=action.__name__):
                work = asyncio.create_task(action(host))
                await asyncio.sleep(0)
                work.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await work


if __name__ == '__main__':
    unittest.main()
