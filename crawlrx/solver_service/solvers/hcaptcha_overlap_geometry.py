"""Measure the observed axis-aligned square/circle overlap puzzle from pixels."""
import itertools
import json
import sys
from pathlib import Path

import cv2
import numpy as np
from numpy.typing import NDArray


def normalized_image(encoded: bytes) -> NDArray[np.uint8]:
    image = cv2.imdecode(np.frombuffer(encoded, np.uint8), cv2.IMREAD_COLOR)
    if image is None:
        raise ValueError('Invalid overlap image')
    height, width = image.shape[:2]
    if min(height, width) < 300 or max(height, width) > 2048 or abs(width / height - 520 / 570) > .004:
        raise ValueError('Unsupported overlap frame dimensions')
    return cv2.resize(image, (520, 570), interpolation=cv2.INTER_AREA)


def marker_center(image: NDArray[np.uint8]) -> list[float]:
    hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
    mask = cv2.inRange(hsv, (0, 0, 0), (179, 255, 100))
    mask[:145] = 0
    mask[465:] = 0
    mask[:, :20] = 0
    mask[:, 490:] = 0
    count, labels, stats, centers = cv2.connectedComponentsWithStats(mask)
    matches = []
    for index in range(1, count):
        x, y, width, height, area = stats[index]
        if not (16 <= width <= 36 and 16 <= height <= 36 and abs(width-height) <= 4 and 80 <= area <= 600):
            continue
        yy, xx = np.where(labels[y:y+height, x:x+width] == index)
        xx = xx / (width-1)
        yy = yy / (height-1)
        first, second = np.abs(xx-yy) < .18, np.abs(xx+yy-1) < .18
        if np.mean(first | second) > .8 and first.sum() > area*.3 and second.sum() > area*.3:
            matches.append(centers[index].tolist())
    if len(matches) != 1:
        raise ValueError('Expected one unambiguous X marker')
    return matches[0]


def _clusters(values):
    groups = []
    for value in sorted(values):
        if not groups or value - np.mean(groups[-1]) > 4:
            groups.append([value])
        else:
            groups[-1].append(value)
    return [float(np.median(group)) for group in groups]


def _support(distance, xs, ys):
    xs, ys = np.rint(xs).astype(int), np.rint(ys).astype(int)
    if np.any(xs < 0) or np.any(xs >= 390) or np.any(ys < 145) or np.any(ys >= 465):
        return 0.0
    return float(np.mean(distance[ys, xs] <= 4))


def outline_masks(image: NDArray[np.uint8]) -> list[NDArray[np.uint8]]:
    hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
    saturation = hsv[:, :, 1].astype(float)
    contrast = saturation-cv2.GaussianBlur(saturation, (21, 21), 0)
    mask = ((saturation >= 45) & ((hsv[:, :, 2] <= 190) | (contrast > 18))).astype('uint8')*255
    masks = [mask]
    for low, high in ((65, 130), (0, 40), (20, 70), (45, 110), (90, 160), (135, 179)):
        masks.append(cv2.inRange(hsv, (low, 35, 20), (high, 255, 235)))
    # Gray outlines have little saturation; use local luminance contrast to
    # isolate their dark stroke without selecting entire background regions.
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY).astype(float)
    masks.append(((cv2.GaussianBlur(gray, (21, 21), 0)-gray) > 18).astype('uint8')*255)
    for mask in masks:
        mask[:145] = 0
        mask[465:] = 0
        mask[:, :20] = 0
        mask[:, 390:] = 0
    return [cv2.subtract(mask, cv2.erode(mask, np.ones((7, 7), np.uint8))) for mask in masks]


def rectangle_candidates(mask: NDArray[np.uint8], fragmented: bool = False) -> list[list[float]]:
    distance = cv2.distanceTransform(255-mask, cv2.DIST_L2, 5)
    # Textured strokes can split a genuine edge into short fragments. These
    # lines only propose coordinates; all four full sides still need support.
    lines = cv2.HoughLinesP(mask, 1, np.pi/180, 30 if fragmented else 50,
                          minLineLength=40 if fragmented else 80, maxLineGap=12)
    if lines is None:
        return []
    horizontal, vertical = [], []
    for x1, y1, x2, y2 in lines.reshape(-1, 4):
        if abs(y1-y2) <= 4:
            horizontal.append((y1+y2)/2)
        if abs(x1-x2) <= 4:
            vertical.append((x1+x2)/2)
    rectangles = []
    for (left, right), (top, bottom) in itertools.product(
            itertools.combinations(_clusters(vertical), 2), itertools.combinations(_clusters(horizontal), 2)):
        width, height = right-left, bottom-top
        if not (80 <= width <= 230 and 80 <= height <= 230 and abs(width-height) < 10):
            continue
        xs = np.concatenate([np.linspace(left, right, 100)]*2 + [np.full(100, left), np.full(100, right)])
        ys = np.concatenate([np.full(100, top), np.full(100, bottom)] + [np.linspace(top, bottom, 100)]*2)
        if _support(distance, xs, ys) > .9:
            rectangles.append([left, top, right, bottom])
    return rectangles


def circle_candidates(mask: NDArray[np.uint8]) -> list[list[float]]:
    distance = cv2.distanceTransform(255-mask, cv2.DIST_L2, 5)
    circles = cv2.HoughCircles(mask, cv2.HOUGH_GRADIENT, dp=1, minDist=60,
                               param1=100, param2=25, minRadius=35, maxRadius=120)
    candidates = []
    angles = np.linspace(0, 2*np.pi, 360, endpoint=False)
    for x, y, radius in [] if circles is None else circles[0]:
        # Hough can favor the inner stroke. Fit the full circumference to the
        # colored outline before deciding whether it describes a real circle.
        fits = []
        for dx, dy, dr in itertools.product(range(-5, 6, 2), repeat=3):
            cx, cy, r = float(x+dx), float(y+dy), float(radius+dr)
            xs, ys = np.rint(cx+r*np.cos(angles)).astype(int), np.rint(cy+r*np.sin(angles)).astype(int)
            if np.any(xs < 20) or np.any(xs >= 390) or np.any(ys < 145) or np.any(ys >= 465):
                continue
            fits.append((float(np.mean(np.minimum(distance[ys, xs], 8))), cx, cy, r))
        if fits:
            _, cx, cy, r = min(fits)
            if _support(distance, cx+r*np.cos(angles), cy+r*np.sin(angles)) > .9:
                candidate = [cx, cy, r]
                if not any(np.linalg.norm(np.array(existing[:2])-candidate[:2]) < 8
                           and abs(existing[2]-r) < 8 for existing in candidates):
                    candidates.append(candidate)
    return candidates


def shape_geometry(image: NDArray[np.uint8]) -> tuple[list[float], list[float]]:
    rectangles, circles, circle_votes = [], [], []
    masks = outline_masks(image)
    for fragmented in (False, True):
        for mask in masks:
            isolated_rectangles = []
            for rectangle in rectangle_candidates(mask, fragmented):
                if not any(np.max(np.abs(np.array(existing)-rectangle)) < 8 for existing in isolated_rectangles):
                    isolated_rectangles.append(rectangle)
            # A hue mask flooded by patterned background produces many geometric
            # votes. It supplies no evidence unless one outline can be isolated.
            if len(isolated_rectangles) == 1:
                rectangle = isolated_rectangles[0]
                if not any(np.max(np.abs(np.array(existing)-rectangle)) < 8 for existing in rectangles):
                    rectangles.append(rectangle)
        if rectangles:
            break
    for mask in masks:
        isolated_circles = circle_candidates(mask)
        for circle in isolated_circles if len(isolated_circles) == 1 else []:
            match = next((index for index, existing in enumerate(circles)
                          if np.linalg.norm(np.array(existing[:2])-circle[:2]) < 8
                          and abs(existing[2]-circle[2]) < 8), None)
            if match is None:
                circles.append(circle)
                circle_votes.append(1)
            else:
                circle_votes[match] += 1
    if len(circles) > 1:
        # Patterned backgrounds can yield one spurious hue-specific circle.
        # Resolve only when exactly one outline is corroborated across masks;
        # two corroborated circles remain ambiguous and must stop input.
        circles = [circle for circle, votes in zip(circles, circle_votes) if votes >= 2]
    if len(rectangles) != 1:
        raise ValueError('Expected one complete square outline')
    if len(circles) != 1:
        raise ValueError('Expected one complete circle outline')
    return rectangles[0], circles[0]


def overlap_plan(encoded: bytes) -> dict[str, list[float] | float]:
    image = normalized_image(encoded)
    marker = marker_center(image)
    if not 390 < marker[0] < 490:
        raise ValueError('X is already moved or outside the initial tray')
    rectangle, circle = shape_geometry(image)
    left, top, right, bottom = rectangle
    cx, cy, radius = circle
    yy, xx = np.mgrid[:570, :520]
    interior = ((xx > left+5) & (xx < right-5) & (yy > top+5) & (yy < bottom-5)
                & ((xx-cx)**2+(yy-cy)**2 < (radius-5)**2))
    clearance = cv2.distanceTransform(interior.astype('uint8'), cv2.DIST_L2, 5)
    _, margin, _, target = cv2.minMaxLoc(clearance)
    if margin < 15:
        raise ValueError('No sufficiently clear shared interior')
    return {'from': marker, 'to': list(target), 'clearance': margin,
            'square': rectangle, 'circle': circle}


def verify_placement(before: bytes, after: bytes) -> dict[str, bool | list[float]]:
    plan = overlap_plan(before)
    image = normalized_image(after)
    marker = marker_center(image)
    left, top, right, bottom = plan['square']
    cx, cy, radius = plan['circle']
    margin = min(marker[0]-left, right-marker[0], marker[1]-top, bottom-marker[1],
                 radius-np.linalg.norm(np.array(marker)-[cx, cy]))
    if margin < 15 or np.linalg.norm(np.array(marker)-plan['from']) < 30:
        raise ValueError('X did not reach the planned shared interior')
    original = normalized_image(before)
    visible = np.zeros((570, 520), dtype=bool)
    visible[145:465, 20:490] = True
    for x, y in (plan['from'], marker):
        visible[max(145, int(y)-65):min(465, int(y)+45), max(20, int(x)-45):min(490, int(x)+45)] = False
    difference = np.max(np.abs(image.astype(float)-original), axis=2)[visible]
    if difference.mean() > 3 or np.mean(difference > 20) > .015:
        raise ValueError('Puzzle background changed after the drag')
    return {'placed': True, 'center': marker}


if __name__ == '__main__':
    try:
        print(json.dumps(overlap_plan(Path(sys.argv[1]).read_bytes())))
    except (ValueError, OSError, IndexError) as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)
