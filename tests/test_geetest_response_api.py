"""GeeTest result bundles remain usable through the compatibility endpoint."""
import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient

from crawlrx.solver_service import main
from crawlrx.solver_service.models import CaptchaMethod, Task, TaskResult, TaskState
from crawlrx.solver_service.task_queue import TaskQueue


class GeeTestResponseTests(unittest.TestCase):
    def setUp(self):
        self.queue = TaskQueue()
        self.patches = [patch.object(main, 'task_queue', self.queue),
                        patch.object(main, 'verify_api_key', return_value=True)]
        for item in self.patches:
            item.start()
            self.addCleanup(item.stop)
        self.client = TestClient(main.app)

    def task(self, method, token):
        self.queue._tasks['fixture'] = Task(
            id='fixture', method=method, params={}, state=TaskState.SOLVED,
            result=TaskResult(success=True, token=token))

    def test_v3_response_in_both_modes(self):
        answer = {'challenge': 'c', 'validate': 'v', 'seccode': 's'}
        self.task(CaptchaMethod.GEETEST, json.dumps(answer))
        for suffix in ('', '&json=1'):
            with self.subTest(suffix=suffix):
                response = self.client.get('/res.php?key=&action=get&id=fixture' + suffix)
                self.assertEqual(response.json(), answer)

    def test_incomplete_bundle_is_rejected(self):
        self.task(CaptchaMethod.GEETEST, json.dumps({'validate': 'v'}))
        response = self.client.get('/res.php?key=&action=get&id=fixture&json=1')
        self.assertEqual(response.json()['request'], 'ERROR_CAPTCHA_UNSOLVABLE')

    def test_other_provider_format_is_unchanged(self):
        self.task(CaptchaMethod.HCAPTCHA, 'ordinary-token')
        url = '/res.php?key=&action=get&id=fixture'
        self.assertEqual(self.client.get(url).text, 'OK|ordinary-token')
        self.assertEqual(self.client.get(url + '&json=1').json()['request'], 'ordinary-token')


if __name__ == '__main__':
    unittest.main()
