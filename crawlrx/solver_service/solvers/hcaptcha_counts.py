"""Validate reference quantities and candidate centers for the observed count puzzle."""
import json
import math
import re
from collections import Counter

INSTRUCTION = 'pick all similar items in the amounts shown'
PROMPT = ('Read the reference items and their required counts on the left. Identify the matching '
          'candidate items on the right, excluding the reference pictures. Return only compact JSON: '
          '{"requirements":[["label",count]],"matches":[["label",x,y]]}. Include every reference '
          'requirement and exactly the required number of matches for each. Coordinates are centers '
          'normalized 0 to 1000 relative to the entire image. Use consistent simple labels. No markdown.')


def is_count_instruction(text: str) -> bool:
    return ' '.join(text.split()).lower().rstrip('.!') == INSTRUCTION


def _label(value):
    if not isinstance(value, str) or not value.strip():
        return None
    label = ' '.join(value.split()).casefold()
    return None if re.search(r'\b(uncertain|unknown|maybe|possibly|unsure)\b', label) else label


def parse_counts(text: str | None) -> list[tuple[float, float]] | None:
    if not isinstance(text, str):
        return None
    try:
        data = json.loads(text)
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    requirements, matches = data.get('requirements'), data.get('matches')
    if (not isinstance(requirements, list) or not 1 <= len(requirements) <= 6
            or not isinstance(matches, list) or not 1 <= len(matches) <= 9):
        return None
    required = {}
    for item in requirements:
        if not isinstance(item, list) or len(item) != 2:
            return None
        label, count = _label(item[0]), item[1]
        if label is None or label in required or type(count) is not int or not 1 <= count <= 9:
            return None
        required[label] = count
    if sum(required.values()) != len(matches):
        return None
    actual, points = Counter(), []
    for item in matches:
        if not isinstance(item, list) or len(item) != 3:
            return None
        label, x, y = _label(item[0]), item[1], item[2]
        if label not in required or any(type(n) not in (int, float) or not math.isfinite(n) for n in (x, y)):
            return None
        x, y = x / 1000, y / 1000
        if not .30 <= x <= .97 or not .22 <= y <= .84:
            return None
        if any(math.hypot(x-a, y-b) < .02 for a, b in points):
            return None
        actual[label] += 1
        points.append((x, y))
    return points if actual == required else None
