"""FunCaptcha must use the speech model shipped in the unified image."""
import os
import shutil
import threading
import time
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch
from concurrent.futures import ThreadPoolExecutor

from crawlrx.solver_service.solvers._funcaptcha_browser import WhisperTranscriber


class FunCaptchaWhisperTests(unittest.TestCase):
    def test_ten_concurrent_loads_share_one_model_initialization(self):
        barrier = threading.Barrier(10)
        backend = MagicMock()

        def construct(*args, **kwargs):
            time.sleep(.05)
            return backend

        transcriber = WhisperTranscriber()

        def load(index):
            barrier.wait(timeout=2)
            return transcriber._load()

        with (patch('faster_whisper.WhisperModel', side_effect=construct) as constructor,
              patch.object(transcriber, '_check_cuda', return_value=False),
              ThreadPoolExecutor(max_workers=10) as workers):
            self.assertEqual(list(workers.map(load, range(10))), [True] * 10)
        constructor.assert_called_once()
        self.assertIs(transcriber._model, backend)

    def test_configured_bundle_is_loaded_locally_and_cached(self):
        backend = MagicMock()
        with (patch.dict(os.environ, {'CRAWLRX_WHISPER_MODEL_DIR': '/bundled/whisper'}),
              patch('faster_whisper.WhisperModel', return_value=backend) as constructor,
              patch.object(WhisperTranscriber, '_check_cuda', return_value=False)):
            transcriber = WhisperTranscriber()
            self.assertTrue(transcriber._load())
            self.assertTrue(transcriber._load())
        constructor.assert_called_once_with(
            '/bundled/whisper', device='cpu', compute_type='int8', local_files_only=True,
        )
        self.assertIs(transcriber._model, backend)

    def test_missing_configured_bundle_does_not_fall_back_to_download(self):
        with (patch.dict(os.environ, {'CRAWLRX_WHISPER_MODEL_DIR': '/missing/whisper'}),
              patch('faster_whisper.WhisperModel', side_effect=OSError('missing model')) as constructor,
              patch.object(WhisperTranscriber, '_check_cuda', return_value=False)):
            transcriber = WhisperTranscriber()
            self.assertFalse(transcriber._load())
            self.assertIsNone(transcriber._model)
        constructor.assert_called_once_with(
            '/missing/whisper', device='cpu', compute_type='int8', local_files_only=True,
        )

    def test_unconfigured_standalone_install_retains_name_loading(self):
        with (patch.dict(os.environ),
              patch('faster_whisper.WhisperModel') as constructor,
              patch.object(WhisperTranscriber, '_check_cuda', return_value=False)):
            os.environ.pop('CRAWLRX_WHISPER_MODEL_DIR', None)
            self.assertTrue(WhisperTranscriber()._load())
        constructor.assert_called_once_with(
            'base', device='cpu', compute_type='int8', local_files_only=False,
        )

    def test_actual_bundled_model_loads_and_transcribes(self):
        bundle = os.environ.get('CRAWLRX_WHISPER_MODEL_DIR')
        if not bundle or not Path(bundle).is_dir():
            self.skipTest('Requires the unified image speech model')
        transcriber = WhisperTranscriber()
        with patch.object(transcriber, '_check_cuda', return_value=False):
            self.assertTrue(transcriber._load())
        audio = Path(__file__).parent / 'fixtures' / 'rotate-right.wav'
        transcript = transcriber.transcribe(str(audio))
        self.assertIsInstance(transcript, str)
        self.assertRegex(transcript.lower(), r'\bright\b')

    def test_recaptcha_also_transcribes_the_bundled_speech_fixture(self):
        bundle = os.environ.get('CRAWLRX_WHISPER_MODEL_DIR')
        if not bundle or not Path(bundle).is_dir():
            self.skipTest('Requires the unified image speech model')
        from crawlrx.solver_service.solvers import _recaptcha_audio_transcribe as recaptcha
        fixture = Path(__file__).parent / 'fixtures' / 'rotate-right.wav'
        with (patch.object(recaptcha, '_WHISPER_MODEL', None),
              patch.object(recaptcha, '_WHISPER_TYPE', None),
              tempfile.TemporaryDirectory() as directory):
            audio = Path(directory) / 'speech.wav'
            shutil.copyfile(fixture, audio)
            transcript = recaptcha.transcribe_audio(str(audio))
            self.assertRegex(transcript, r'\bright\b')
            self.assertFalse(audio.exists())


if __name__ == '__main__':
    unittest.main()
