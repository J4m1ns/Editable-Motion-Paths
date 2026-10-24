"""Easing curves (Robert Penner's, the same as Blender's easing interpolation types): share of
the way covered after the share u of the time. Used by the easing presets and their icons."""

import math

# (identifier, name): the identifiers are Blender's keyframe interpolation types
TYPES = [
    ('SINE', "Sine"), ('QUAD', "Quadratic"), ('CUBIC', "Cubic"), ('QUART', "Quartic"),
    ('QUINT', "Quintic"), ('EXPO', "Exponential"), ('CIRC', "Circular"), ('BACK', "Back"),
    ('BOUNCE', "Bounce"), ('ELASTIC', "Elastic"),
]
DIRECTIONS = [('EASE_IN', "In"), ('EASE_OUT', "Out"), ('EASE_IN_OUT', "In & Out")]


def _bounce_out(u, bounces=3, bounciness=0.5):
    """A ball dropped onto the end: it falls, then bounces `bounces` times, each bounce
    `bounciness` as long (and its square as high) as the one before. 3 and 0.5 give the
    classic Bounce."""
    e = min(max(bounciness, 0.0), 0.95)
    n = max(0, int(bounces))
    a = 1.0 / (1.0 + 2.0 * sum(e ** i for i in range(1, n + 1)))     # time of the fall
    if u < a:
        return (u / a) ** 2
    t = u - a
    for i in range(1, n + 1):
        half = a * e ** i
        if t < 2.0 * half:
            x = (t - half) / half
            return 1.0 - e ** (2 * i) * (1.0 - x * x)
        t -= 2.0 * half
    return 1.0


def _in(kind, u, back, amplitude, period, bounces=3, bounciness=0.5):
    if kind == 'SINE':
        return 1.0 - math.cos(u * math.pi * 0.5)
    if kind == 'QUAD':
        return u ** 2
    if kind == 'CUBIC':
        return u ** 3
    if kind == 'QUART':
        return u ** 4
    if kind == 'QUINT':
        return u ** 5
    if kind == 'EXPO':
        return 0.0 if u <= 0.0 else 2.0 ** (10.0 * (u - 1.0))
    if kind == 'CIRC':
        return 1.0 - math.sqrt(max(0.0, 1.0 - u * u))
    if kind == 'BACK':
        return u * u * ((back + 1.0) * u - back)
    if kind == 'BOUNCE':
        return 1.0 - _bounce_out(1.0 - u, bounces, bounciness)
    if kind == 'ELASTIC':
        if u <= 0.0 or u >= 1.0:
            return u
        a = max(amplitude, 1.0)
        s = period / (2.0 * math.pi) * math.asin(1.0 / a)
        return -a * 2.0 ** (10.0 * (u - 1.0)) * math.sin((u - 1.0 - s) * 2.0 * math.pi / period)
    return u


def ease(kind, direction, u, back=1.70158, amplitude=1.0, period=0.3, bounces=3, bounciness=0.5):
    """Progress (0 to 1, Back and Elastic go past it) after the share u of the time."""
    u = min(1.0, max(0.0, u))
    p = (back, amplitude, period, bounces, bounciness)
    if direction == 'EASE_IN':
        return _in(kind, u, *p)
    if direction == 'EASE_OUT':
        return 1.0 - _in(kind, 1.0 - u, *p)
    if u < 0.5:
        return 0.5 * _in(kind, 2.0 * u, *p)
    return 1.0 - 0.5 * _in(kind, 2.0 - 2.0 * u, *p)
