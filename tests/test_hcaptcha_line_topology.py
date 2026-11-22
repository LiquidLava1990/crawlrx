"""Regression coverage for line-puzzle connectivity."""
import cv2
import numpy as np
import pytest

from crawlrx.solver_service.solvers.hcaptcha_line_geometry import line_plan


def puzzle(last_piece):
    image = np.full((570, 520, 3), 255, dtype=np.uint8)
    pieces = [((100, 190), (100, 245)), ((170, 190), (190, 245)),
              last_piece, ((405, 340), (475, 340)), ((400, 420), (490, 420))]
    for start, end in pieces:
        cv2.line(image, start, end, (0, 0, 120), 7)
    ok, data = cv2.imencode('.png', image)
    assert ok
    return data.tobytes()


def test_rejects_disconnected_line_with_two_links_between_same_pieces():
    with pytest.raises(ValueError, match='Line placement is ambiguous'):
        line_plan(puzzle(((300, 190), (300, 245))))


def test_accepts_links_joining_all_three_fixed_pieces():
    actions = line_plan(puzzle(((300, 190), (280, 245))))
    assert len(actions) == 2
    assert actions[0]['to'][0] == pytest.approx(135, abs=4)
    assert actions[1]['to'][0] == pytest.approx(235, abs=4)


@pytest.mark.parametrize('detector', [line_plan])
@pytest.mark.parametrize('scale', [1.25, 1.5, 2])
def test_scaled_ss_screenshot_keeps_drag_positions_in_css_pixels(detector, scale):
    raw = puzzle(((300, 190), (280, 245)))
    image = cv2.imdecode(np.frombuffer(raw, np.uint8), cv2.IMREAD_COLOR)
    image = cv2.resize(image, (int(np.ceil(520*scale)), int(np.ceil(570*scale))))
    success, encoded = cv2.imencode('.jpg', image, [cv2.IMWRITE_JPEG_QUALITY, 90])
    assert success
    actions = detector(encoded.tobytes())
    assert len(actions) == 2
    for actual, expected in zip(actions, line_plan(raw)):
        assert actual['from'] == pytest.approx(expected['from'], abs=4)
        assert actual['to'] == pytest.approx(expected['to'], abs=4)


@pytest.mark.parametrize('detector', [line_plan])
@pytest.mark.parametrize('size', [(650, 500), (260, 285), (2600, 2850)])
def test_incompatible_or_unbounded_image_sizes_are_not_stretched_into_a_plan(detector, size):
    image = np.full((size[1], size[0], 3), 255, dtype=np.uint8)
    success, encoded = cv2.imencode('.png', image)
    assert success
    with pytest.raises(ValueError, match='dimensions'):
        detector(encoded.tobytes())
