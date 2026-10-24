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


def _bounce_out(u):
    n, d = 7.5625, 2.75
    if u < 1.0 / d:
        return n * u * u
    if u < 2.0 / d:
        u -= 1.5 / d
        return n * u * u + 0.75
    if u < 2.5 / d:
        u -= 2.25 / d
        return n * u * u + 0.9375
    u -= 2.625 / d
    return n * u * u + 0.984375


def _in(kind, u, back, amplitude, period):
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
        return 1.0 - _bounce_out(1.0 - u)
    if kind == 'ELASTIC':
        if u <= 0.0 or u >= 1.0:
            return u
        a = max(amplitude, 1.0)
        s = period / (2.0 * math.pi) * math.asin(1.0 / a)
        return -a * 2.0 ** (10.0 * (u - 1.0)) * math.sin((u - 1.0 - s) * 2.0 * math.pi / period)
    return u


def ease(kind, direction, u, back=1.70158, amplitude=1.0, period=0.3):
    """Progress (0 to 1, Back and Elastic go past it) after the share u of the time."""
    u = min(1.0, max(0.0, u))
    if direction == 'EASE_IN':
        return _in(kind, u, back, amplitude, period)
    if direction == 'EASE_OUT':
        return 1.0 - _in(kind, 1.0 - u, back, amplitude, period)
    if u < 0.5:
        return 0.5 * _in(kind, 2.0 * u, back, amplitude, period)
    return 1.0 - 0.5 * _in(kind, 2.0 - 2.0 * u, back, amplitude, period)
