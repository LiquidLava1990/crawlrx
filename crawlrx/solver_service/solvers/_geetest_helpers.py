"""GeeTest solver helpers — selectors, JS payloads, math, encoding.

Split out of ``geetest_solver.py`` so the orchestrator stays under the
500-LOC ceiling. Pure functions / constants only — no browser or
network state lives here.
"""
from __future__ import annotations

import random
from typing import Dict, List, Tuple


# ---------------------------------------------------------------------------
# Selector tables (used by detection + slider/icon strategies)
# ---------------------------------------------------------------------------
GEETEST_SELECTORS: Dict[str, str] = {
    "v3_slider": ".gt_slider_knob, .gt_slider, .geetest_slider",
    "v4_slider": ".geetest_btn, .geetest_slider_btn, [class*='geetest-btn']",
    "v3_puzzle": ".gt_box, .gt_widget, .geetest_widget, .geetest_box",
    "v4_puzzle": ".geetest_box, .geetest_container, [class*='geetest-box']",
    "captcha_slice": ".gt_slice, .geetest_slice, [class*='slice']",
    "captcha_bg": ".gt_box .gt_bg, .geetest_bg, .geetest_canvas_bg, canvas",
    "icon_challenge": ".geetest_icons, .geetest_item, .geetest_icons_item",
    "space_challenge": ".geetest_space, .geetest_gap",
    "success_indicator": ".geetest_success, .geetest_radar, .gt_success",
    "reset_btn": ".geetest_reset, .gt_refresh",
}

# Probe order used by ``_detect_captcha_type``: each tuple is
# ``(selector_key, label_returned)``. Order matters — earlier matches win.
GEETEST_TYPE_PROBES: Tuple[Tuple[str, str], ...] = (
    ("icon_challenge", "icon"),
    ("space_challenge", "space"),
    ("v3_slider", "slider"),
    ("v4_slider", "slider"),
)


# ---------------------------------------------------------------------------
# JavaScript payloads
# ---------------------------------------------------------------------------
V4_CAPTURE_INIT: str = """
    (() => {
        const records = [];
        Object.defineProperty(window, '__crawlrxGeeTest4', {value: records});
        let current;
        Object.defineProperty(window, 'initGeetest4', {
            configurable: true,
            get() { return current; },
            set(original) {
                if (typeof original !== 'function') { current = original; return; }
                current = function(config, callback, ...rest) {
                    if (typeof callback !== 'function') return original.call(this, config, callback, ...rest);
                    const capture = function(widget, ...args) {
                        records.push({id: config?.captchaId, widget});
                        return callback.call(this, widget, ...args);
                    };
                    return original.call(this, config, capture, ...rest);
                };
            }
        });
    })();
"""

TOKEN_JS: str = """
    (expected) => {
        if (expected.version === 4) {
            if (typeof expected.captchaId !== 'string' || !expected.captchaId) return null;
            const matches = (window.__crawlrxGeeTest4 || []).filter(record => record.id === expected.captchaId);
            if (matches.length !== 1 || typeof matches[0].widget?.getValidate !== 'function') return null;
            let result;
            try { result = matches[0].widget.getValidate(); } catch { return null; }
            if (!result || typeof result !== 'object' || Array.isArray(result)) return null;
            return {captcha_id: matches[0].id, lot_number: result.lot_number,
                    pass_token: result.pass_token, gen_time: result.gen_time, captcha_output: result.captcha_output};
        }
        const names = ['geetest_challenge', 'geetest_validate', 'geetest_seccode'];
        const fromObject = window.geetest_token;
        if (fromObject && typeof fromObject === 'object' && !Array.isArray(fromObject)) {
            const result = {challenge: fromObject.geetest_challenge ?? fromObject.challenge,
                            validate: fromObject.geetest_validate ?? fromObject.validate,
                            seccode: fromObject.geetest_seccode ?? fromObject.seccode};
            if (Object.values(result).every(value => typeof value === 'string' && value.trim())) return result;
        }
        const matches = names.map(name => document.querySelectorAll(`[name="${name}"]`));
        if (matches.some(nodes => nodes.length !== 1)) return null;
        const fields = matches.map(nodes => nodes[0]);
        if (fields.some(field => field.form !== fields[0].form)) return null;
        return {challenge: fields[0].value, validate: fields[1].value, seccode: fields[2].value};
    }
"""


ANTI_DETECTION_JS: str = """
    Object.defineProperty(navigator, 'webdriver', {
        get: () => undefined
    });

    Object.defineProperty(navigator, 'plugins', {
        get: () => [1, 2, 3, 4, 5]
    });

    window.chrome = { runtime: {} };

    Object.defineProperty(navigator, 'languages', {
        get: () => ['en-US', 'en']
    });
"""


# ---------------------------------------------------------------------------
# Pure math helpers
# ---------------------------------------------------------------------------
def ease_in_out_cubic(t: float) -> float:
    """Cubic easing function for natural acceleration/deceleration."""
    if t < 0.5:
        return 4 * t * t * t
    return 1 - pow(-2 * t + 2, 3) / 2


def generate_bezier_points(
    start: float, end: float, num_points: int = 20,
) -> List[float]:
    """Generate points along a cubic bezier curve for natural acceleration."""
    p0 = start
    p1 = start + (end - start) * 0.3
    p2 = start + (end - start) * 0.7
    p3 = end
    points: List[float] = []
    for i in range(num_points):
        t = i / (num_points - 1)
        t = ease_in_out_cubic(t)
        x = (
            (1 - t) ** 3 * p0
            + 3 * (1 - t) ** 2 * t * p1
            + 3 * (1 - t) * t ** 2 * p2
            + t ** 3 * p3
        )
        points.append(x)
    return points


# ---------------------------------------------------------------------------
# Encoding + tracking
# ---------------------------------------------------------------------------
def encode_response(distance: int, challenge: str) -> str:
    """Encode the slide distance as a (simplified) GeeTest userresponse."""
    challenge_len = len(challenge) if challenge else 32
    pos = int((distance / 260) * challenge_len) % challenge_len
    return challenge[:pos] if pos < challenge_len else challenge[:8]


def generate_aa_string() -> str:
    """Generate a random 'aa' tracking string for GeeTest."""
    chars = (
        "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
        "abcdefghijklmnopqrstuvwxyz"
        "0123456789"
    )
    return "".join(random.choice(chars) for _ in range(random.randint(50, 100)))


__all__ = [
    "GEETEST_SELECTORS",
    "GEETEST_TYPE_PROBES",
    "TOKEN_JS",
    "ANTI_DETECTION_JS",
    "ease_in_out_cubic",
    "generate_bezier_points",
    "encode_response",
    "generate_aa_string",
]
