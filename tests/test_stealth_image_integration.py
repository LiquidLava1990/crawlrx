import asyncio
from unittest.mock import AsyncMock, Mock, patch

from crawlrx.solver_service.botright_patches import apply_all_patches
from crawlrx.solver_service.solvers import stealth_solver

apply_all_patches()


def test_availability_tracks_lane_and_enabled_state():
    async def check():
        # Lane contract: healthy == (own engine ready OR legacy botright lane
        # installed). Force each factor and assert the boolean algebra.
        with patch.object(stealth_solver, '_own_engine_ready', return_value=True),              patch.object(stealth_solver, 'BOTRIGHT_AVAILABLE', False):
            solver = stealth_solver.StealthSolver({'enabled': True})
            assert solver.health_check()['healthy'] is True
            assert await solver.is_available() is True
        with patch.object(stealth_solver, '_own_engine_ready', return_value=False),              patch.object(stealth_solver, 'BOTRIGHT_AVAILABLE', True):
            solver = stealth_solver.StealthSolver({'enabled': True})
            assert solver.health_check()['healthy'] is True
            assert await solver.is_available() is True
        with patch.object(stealth_solver, '_own_engine_ready', return_value=False),              patch.object(stealth_solver, 'BOTRIGHT_AVAILABLE', False):
            solver = stealth_solver.StealthSolver({'enabled': True})
            assert solver.health_check()['healthy'] is False
            assert await solver.is_available() is False
        with patch.object(stealth_solver, '_own_engine_ready', return_value=True),              patch.object(stealth_solver, 'BOTRIGHT_AVAILABLE', True):
            assert not await stealth_solver.StealthSolver({'enabled': False}).is_available()
    asyncio.run(check())


def test_recaptcha_bridge_drives_audio_challenge_without_reloading_page():
    from botright.modules import recaptcha_bridge as bridge

    async def check():
        page = object()
        audio_state = object()
        with patch.object(bridge, 'extract_token', AsyncMock(side_effect=[None, 'provider-token'])), \
             patch.object(bridge, 'click_recaptcha_checkbox', AsyncMock()) as click, \
             patch.object(bridge, 'is_challenge_visible', AsyncMock(return_value=True)), \
             patch.object(bridge, 'switch_to_audio_challenge', AsyncMock()) as switch, \
             patch.object(bridge, 'capture_audio_state', AsyncMock(return_value=audio_state)), \
             patch.object(bridge, 'get_audio_challenge', AsyncMock(return_value='/tmp/challenge.mp3')), \
             patch.object(bridge, 'transcribe_audio_async', AsyncMock(return_value='answer')) as transcribe, \
             patch.object(bridge, 'submit_audio_response', AsyncMock()) as submit, \
             patch.object(bridge.asyncio, 'sleep', AsyncMock()):
            assert await bridge.solve_recaptcha(page) == 'provider-token'
            click.assert_awaited_once_with(page)
            switch.assert_awaited_once_with(page)
            transcribe.assert_awaited_once_with('/tmp/challenge.mp3')
            submit.assert_awaited_once_with(page, 'answer', expected_state=audio_state)
    asyncio.run(check())
