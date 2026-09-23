"""
iso286.py - ISO 286 limits & fits lookup (metric table, inch conversion)
=========================================================================
Converts a fit designation on a nominal size (e.g. 1.000" F7, 25 h6) into its
upper/lower deviations.  Values are the standard ISO 286-2 table values in
micrometres for nominal sizes 0-500 mm.

Coverage (the classes used on shop drawings):
  shafts : d e f g h js k m n p
  holes  : D E F G H JS K M N P
  grades : IT3 - IT13
Anything else returns None rather than a guess.
"""

import re

# Nominal size ranges in mm (upper bound inclusive; first range is 0 < D <= 3)
_RANGES = [3, 6, 10, 18, 30, 50, 80, 120, 180, 250, 315, 400, 500]

# Standard tolerance grades IT3..IT13, micrometres, one row per size range
_IT = {
    3:  [2, 2.5, 2.5, 3, 4, 4, 5, 6, 8, 10, 12, 13, 15],
    4:  [3, 4, 4, 5, 6, 7, 8, 10, 12, 14, 16, 18, 20],
    5:  [4, 5, 6, 8, 9, 11, 13, 15, 18, 20, 23, 25, 27],
    6:  [6, 8, 9, 11, 13, 16, 19, 22, 25, 29, 32, 36, 40],
    7:  [10, 12, 15, 18, 21, 25, 30, 35, 40, 46, 52, 57, 63],
    8:  [14, 18, 22, 27, 33, 39, 46, 54, 63, 72, 81, 89, 97],
    9:  [25, 30, 36, 43, 52, 62, 74, 87, 100, 115, 130, 140, 155],
    10: [40, 48, 58, 70, 84, 100, 120, 140, 160, 185, 210, 230, 250],
    11: [60, 75, 90, 110, 130, 160, 190, 220, 250, 290, 320, 360, 400],
    12: [100, 120, 150, 180, 210, 250, 300, 350, 400, 460, 520, 570, 630],
    13: [140, 180, 220, 270, 330, 390, 460, 540, 630, 720, 810, 890, 970],
}

# Shaft fundamental deviations, micrometres.
# Upper deviation es (a..h, negative or zero):
_SHAFT_ES = {
    "d": [-20, -30, -40, -50, -65, -80, -100, -120, -145, -170, -190, -210, -230],
    "e": [-14, -20, -25, -32, -40, -50, -60, -72, -85, -100, -110, -125, -135],
    "f": [-6, -10, -13, -16, -20, -25, -30, -36, -43, -50, -56, -62, -68],
    "g": [-2, -4, -5, -6, -7, -9, -10, -12, -14, -15, -17, -18, -20],
    "h": [0] * 13,
}
# Lower deviation ei (k..p, positive):
_SHAFT_EI = {
    "k": [0, 1, 1, 1, 2, 2, 2, 3, 3, 4, 4, 4, 5],    # grades 4-7; 0 for <=3 and >7
    "m": [2, 4, 6, 7, 8, 9, 11, 13, 15, 17, 20, 21, 23],
    "n": [4, 8, 10, 12, 15, 17, 20, 23, 27, 31, 34, 37, 40],
    "p": [6, 12, 15, 18, 22, 26, 32, 37, 43, 50, 56, 62, 68],
}

MM_PER_IN = 25.4


def _range_idx(d_mm):
    if d_mm <= 0 or d_mm > 500:
        return None
    for i, ub in enumerate(_RANGES):
        if d_mm <= ub:
            return i
    return None


def _it(grade, idx):
    row = _IT.get(grade)
    return None if row is None else row[idx]


def deviations_um(nominal_mm, fit_class):
    """Return (upper, lower) deviation in micrometres, or None if not covered."""
    m = re.fullmatch(r'([A-Za-z]{1,2})(\d{1,2})', fit_class.strip())
    if not m:
        return None
    letter, grade = m.group(1), int(m.group(2))
    idx = _range_idx(nominal_mm)
    it = _it(grade, idx) if idx is not None else None
    if it is None:
        return None
    is_hole = letter.isupper()
    L = letter.lower()

    # --- shaft deviations first (holes are derived from them) ---
    if L == "js":
        return (it / 2, -it / 2)
    if L in _SHAFT_ES:
        es = _SHAFT_ES[L][idx]
        s_upper, s_lower = es, es - it
    elif L in _SHAFT_EI:
        ei = _SHAFT_EI[L][idx]
        if L == "k" and not (4 <= grade <= 7):
            ei = 0
        s_upper, s_lower = ei + it, ei
    else:
        return None

    if not is_hole:
        return (s_upper, s_lower)

    # --- holes ---
    if L in _SHAFT_ES:                       # D..H: EI = -es
        EI = -_SHAFT_ES[L][idx]
        return (EI + it, EI)
    # K, M, N, P: ES = -ei (+ delta for fine grades above 3 mm)
    ei = _SHAFT_EI[L][idx]
    delta = 0
    if nominal_mm > 3:
        prev = _it(grade - 1, idx)
        if prev is not None:
            if (L in ("k", "m", "n") and grade <= 8) or (L == "p" and grade <= 7):
                delta = it - prev
    if L == "k":
        ES = (-ei + delta) if grade <= 8 else 0
    elif L == "n" and grade >= 9:
        ES = 0
    else:
        ES = -ei + delta
    return (ES, ES - it)


def fit_limits_in(nominal_in, fit_class):
    """ISO limits for an inch nominal: {'upper_in','lower_in','band_in','kind'} or None."""
    try:
        d_mm = float(nominal_in) * MM_PER_IN
    except (TypeError, ValueError):
        return None
    dev = deviations_um(d_mm, fit_class)
    if dev is None:
        return None
    up, lo = dev
    return {
        "upper_in": round(up / 1000 / MM_PER_IN, 5),
        "lower_in": round(lo / 1000 / MM_PER_IN, 5),
        "band_in": round((up - lo) / 1000 / MM_PER_IN, 5),
        "upper_um": up, "lower_um": lo,
        "kind": "hole" if fit_class.strip()[0].isupper() else "shaft",
    }


if __name__ == "__main__":
    # Spot checks against ISO 286-2 published values (mm)
    checks = {
        ("25", "H7"): (21, 0), ("25", "k6"): (15, 2), ("25", "N7"): (-7, -28),
        ("25", "P7"): (-14, -35), ("25", "M7"): (0, -21), ("25", "K7"): (6, -15),
        ("25", "f7"): (-20, -41), ("25", "F7"): (41, 20), ("50", "g6"): (-9, -25),
        ("100", "h6"): (0, -22), ("10", "js6"): (4.5, -4.5), ("60", "p6"): (51, 32),
        ("40", "e8"): (-50, -89), ("150", "H8"): (63, 0), ("12", "n6"): (23, 12),
    }
    bad = 0
    for (d, c), exp in checks.items():
        got = deviations_um(float(d), c)
        flag = "OK " if got == exp else "BAD"
        bad += got != exp
        print(flag, d, c, got, exp)
    print("failures:", bad)
