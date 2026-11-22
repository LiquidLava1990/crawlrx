import asyncio
import threading
from unittest.mock import AsyncMock
import pytest
from playwright.async_api import Error as PlaywrightError
from crawlrx.solver_service.solvers import hcaptcha_line as line


class Mouse:
    def __init__(self, failure=None):
        self.moves = 0
        self.positions = []
        self.downs = 0
        self.ups = 0
        self.failure = failure

    async def move(self, *args, **kwargs):
        self.moves += 1
        self.positions.append(args)
        if self.moves == 2 and self.failure:
            raise self.failure

    async def click(self, *args):
        self.positions.append(args)

    async def down(self):
        self.downs += 1

    async def up(self):
        self.ups += 1


class Button:
    first = property(lambda self: self)

    async def count(self): return 1
    async def is_visible(self): return True
    async def get_attribute(self, name): return 'Verify Answers'
    async def click(self, **kwargs): pass


class Frame:
    def get_by_role(self, *args, **kwargs): return Button()


class Element:
    async def screenshot(self, **kwargs): return b'same puzzle'


class Page:
    def __init__(self, failure=None): self.mouse = Mouse(failure)
    async def wait_for_timeout(self, delay): pass


@pytest.fixture(autouse=True)
def isolate_capture_and_control_boundaries(monkeypatch):
    from crawlrx.solver_service.solvers import hcaptcha_overlap, hcaptcha_ring
    async def capture(page, frame, element, box):
        return await element.screenshot()
    async def click(page, frame, button):
        await button.click()
    for module, name in [(line, 'capture_frame'), (hcaptcha_overlap, 'capture_frame'),
                         (hcaptcha_ring, 'capture_view')]:
        monkeypatch.setattr(module, name, capture)
        monkeypatch.setattr(module, 'click_control', click)


def prepare(monkeypatch, scale=1):
    frame, element = Frame(), Element()
    async def observe(page):
        return frame, element, {'x': 30, 'y': 40, 'width': 520*scale, 'height': 570*scale}, {'x': 0, 'y': 0, 'w': 520, 'h': 570}
    monkeypatch.setattr(line, 'observe_line', observe)
    monkeypatch.setattr(line, 'line_plan', lambda _: [{'from': [1, 2], 'to': [3, 4]}])


@pytest.mark.parametrize('failure', [PlaywrightError('drag failed'), asyncio.CancelledError()])
def test_interrupted_drag_releases_mouse(monkeypatch, failure):
    prepare(monkeypatch)
    page = Page(failure)
    solver = line.HcaptchaLineSolver()
    if isinstance(failure, asyncio.CancelledError):
        with pytest.raises(asyncio.CancelledError):
            asyncio.run(solver.try_line(page))
    else:
        assert asyncio.run(solver.try_line(page)) is False
    assert page.mouse.downs == 1
    assert page.mouse.ups == 1
    assert solver._done


def test_normal_drag_releases_mouse_and_submits(monkeypatch):
    prepare(monkeypatch)
    page = Page()
    assert asyncio.run(line.HcaptchaLineSolver().try_line(page)) is True
    assert page.mouse.downs == page.mouse.ups == 1


def _prepare_drag_solver(monkeypatch, kind):
    if kind == 'line':
        prepare(monkeypatch)
        return line, line.HcaptchaLineSolver(), 'try_line'
    from crawlrx.solver_service.solvers import hcaptcha_overlap as overlap
    frame, element = Frame(), Element()
    async def observe(page):
        return frame, element, {'x': 30, 'y': 40, 'width': 520, 'height': 570}, {'x': 0, 'y': 0, 'w': 1400, 'h': 1400}
    monkeypatch.setattr(overlap, 'observe_overlap', observe)
    monkeypatch.setattr(overlap, 'overlap_plan', lambda _: {'from': [1, 2], 'to': [3, 4]})
    monkeypatch.setattr(overlap, 'verify_placement', lambda *args: None)
    return overlap, overlap.HcaptchaOverlapSolver(), 'try_overlap'


@pytest.mark.parametrize('kind', ['line', 'overlap'])
@pytest.mark.parametrize('original', [asyncio.CancelledError, RuntimeError])
@pytest.mark.parametrize('release', [PlaywrightError, RuntimeError])
def test_release_failure_preserves_interruption_and_never_replays(monkeypatch, kind, original, release):
    module, solver, method = _prepare_drag_solver(monkeypatch, kind)
    failure = original('interrupted input')
    page = Page(failure)
    page.mouse.up = AsyncMock(side_effect=release('release failed'))
    verify = AsyncMock()
    monkeypatch.setattr(module, 'click_control', verify)
    with pytest.raises(original) as caught:
        asyncio.run(getattr(solver, method)(page))
    assert caught.value is failure
    assert page.mouse.downs == 1
    page.mouse.up.assert_awaited_once()
    verify.assert_not_awaited()
    assert asyncio.run(getattr(solver, method)(page)) is False
    assert page.mouse.downs == 1


@pytest.mark.parametrize('kind', ['line', 'overlap'])
def test_release_failure_after_completed_drag_prevents_verification(monkeypatch, kind):
    module, solver, method = _prepare_drag_solver(monkeypatch, kind)
    page = Page()
    page.mouse.up = AsyncMock(side_effect=PlaywrightError('release failed'))
    verify = AsyncMock()
    monkeypatch.setattr(module, 'click_control', verify)
    assert asyncio.run(getattr(solver, method)(page)) is False
    page.mouse.up.assert_awaited_once()
    verify.assert_not_awaited()
    assert solver._done


@pytest.mark.parametrize('scale', [.8, 1, 1.25, 1.5, 2])
def test_drag_maps_canonical_pixels_to_live_frame(monkeypatch, scale):
    prepare(monkeypatch, scale)
    page = Page()
    assert asyncio.run(line.HcaptchaLineSolver().try_line(page)) is True
    assert page.mouse.positions == [(30+scale, 40+2*scale), (30+3*scale, 40+4*scale)]


@pytest.mark.parametrize('scale', [.8, 1, 1.25, 1.5, 2])
def test_overlap_maps_canonical_pixels_to_live_frame(monkeypatch, scale):
    from crawlrx.solver_service.solvers import hcaptcha_overlap as overlap
    frame, element = Frame(), Element()
    async def observe(page):
        return frame, element, {'x': 30, 'y': 40, 'width': 520*scale, 'height': 570*scale}, {'x': 0, 'y': 0, 'w': 1400, 'h': 1400}
    monkeypatch.setattr(overlap, 'observe_overlap', observe)
    monkeypatch.setattr(overlap, 'overlap_plan', lambda _: {'from': [1, 2], 'to': [3, 4]})
    monkeypatch.setattr(overlap, 'verify_placement', lambda *args: None)
    page = Page()
    assert asyncio.run(overlap.HcaptchaOverlapSolver().try_overlap(page)) is True
    assert page.mouse.positions == [(30+scale, 40+2*scale), (30+3*scale, 40+4*scale)]


@pytest.mark.parametrize('scale', [.8, 1, 1.25, 1.5, 2])
def test_ring_maps_canonical_centers_to_live_frame(monkeypatch, scale):
    from crawlrx.solver_service.solvers import hcaptcha_ring as ring
    frame, element = Frame(), Element()
    async def observe(page):
        return frame, element, {'x': 30, 'y': 40, 'width': 500*scale, 'height': 470*scale}, {'x': 0, 'y': 0, 'w': 1400, 'h': 1400}
    monkeypatch.setattr(ring, 'observe_ring', observe)
    monkeypatch.setattr(ring, 'find_unlinked', lambda _: [(100, 200), (300, 400)])
    page = Page()
    assert asyncio.run(ring.HcaptchaRingSolver().try_ring(page)) is True
    assert page.mouse.positions == [(30+100*scale, 40+200*scale), (30+300*scale, 40+400*scale)]


@pytest.mark.parametrize('cancel', [False, True])
def test_ring_analysis_keeps_other_sessions_responsive(monkeypatch, cancel):
    from crawlrx.solver_service.solvers import hcaptcha_ring as ring
    frame, element = Frame(), Element()
    started, release = threading.Event(), threading.Event()
    async def observe(page):
        return frame, element, {'x': 30, 'y': 40, 'width': 500, 'height': 470}, {'x': 0, 'y': 0, 'w': 1400, 'h': 1400}
    def detector(_image):
        started.set()
        release.wait(2)
        return [(100, 200), (300, 400)]
    monkeypatch.setattr(ring, 'observe_ring', observe)
    monkeypatch.setattr(ring, 'find_unlinked', detector)
    async def scenario():
        target = Page()
        task = asyncio.create_task(ring.HcaptchaRingSolver().try_ring(target))
        try:
            async with asyncio.timeout(3):
                while not started.is_set():
                    await asyncio.sleep(0)
            # Another session must run while image analysis is still waiting.
            assert not task.done()
            assert target.mouse.positions == []
            if cancel:
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
                assert target.mouse.positions == []
            else:
                release.set()
                assert await task is True
                assert target.mouse.positions == [(130, 240), (330, 440)]
        finally:
            release.set()
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
    asyncio.run(scenario())
