"""
CMC Shop Cost Engine v3.0
Estimates fabrication cost from extracted geometry.

v3.0 upgrades:
  - Assist gas auto-selection (O2 / N2 / Compressed Air 22TK) with burden breakdown
  - Hole quality warnings (> 7ga needs drill press chasing for precision holes)
  - Welding bench/off-bench/fixture setup logic
  - Grizzly flap wheel deburr as middle option
  - Guifil brake routing for lighter-duty bends
  - Setup time itemization (programming, material pull, staging, QA)
  - Machine capacity validation warnings

All rates/speeds/times are loaded from a config dict (editable via the Shop Rates
admin panel).  When no config is supplied, DEFAULT_CONFIG is used.
"""
import copy

# ═══════════════════════════════════════════════════════════════════
#  DEFAULT CONFIGURATION  (seed values — overridden by admin panel)
# ═══════════════════════════════════════════════════════════════════

DEFAULT_CONFIG = {
    # ── Machine Rates ($/hr) ──────────────────────────────────────
    "rates": {
        "laser":          171.00,
        "water_jet":      178.00,
        "brake_adira":    157.27,
        "brake_guifil":   146.51,
        "st30_turning":   152.27,
        "tm2p_milling":   166.06,
        "tl2_turning":    125.45,
        "shear":          146.51,
        "tig_weld":       131.11,
        "mig_weld":       133.81,
        "laser_weld":     129.53,
        "deburr_apex":    125.76,
        "deburr_manual":  125.45,
        "deburr_hand":    125.00,
        "passivation":    125.30,
        "sheet_handling":  125.00,
        "sawing":         125.76,
        "labor_only":     125.00,
    },

    # ── Setup Times (hr) ──────────────────────────────────────────
    "setup": {
        "laser":         0.55,
        "water_jet":     0.55,
        "brake_adira":   0.55,
        "brake_guifil":  0.55,
        "st30_turning":  0.65,
        "tm2p_milling":  2.15,
        "tl2_turning":   0.65,
        "shear":         0.35,
        "tig_weld":      0.30,
        "mig_weld":      0.30,
        "laser_weld":    0.30,
        "deburr_apex":   0.25,
        "deburr_hand":   0.25,
        "sawing":        0.30,
    },

    # ── Laser Cut Speeds (IPM) ────────────────────────────────────
    # key = "material|thickness_in"  e.g. "carbon_steel|0.125"
    "laser_speeds": {
        "carbon_steel|0.030":    550,
        "carbon_steel|0.048":    480,
        "carbon_steel|0.060":    420,
        "carbon_steel|0.075":    350,
        "carbon_steel|0.090":    290,
        "carbon_steel|0.120":    220,
        "carbon_steel|0.125":    320,
        "carbon_steel|0.1875":   140,
        "carbon_steel|0.250":    160,
        "carbon_steel|0.375":     55,
        "carbon_steel|0.500":     80,
        "carbon_steel|0.750":     48,
        "carbon_steel|1.000":     28,
        "stainless_steel|0.030":  450,
        "stainless_steel|0.048":  380,
        "stainless_steel|0.060":  320,
        "stainless_steel|0.075":  260,
        "stainless_steel|0.090":  210,
        "stainless_steel|0.120":  160,
        "stainless_steel|0.125":  240,
        "stainless_steel|0.1875": 100,
        "stainless_steel|0.250":  112,
        "stainless_steel|0.375":   35,
        "stainless_steel|0.500":   48,
        "stainless_steel|0.750":   24,
        "stainless_steel|1.000":   14.4,
        "aluminum|0.030":         700,
        "aluminum|0.048":         600,
        "aluminum|0.060":         520,
        "aluminum|0.075":         440,
        "aluminum|0.090":         380,
        "aluminum|0.120":         300,
        "aluminum|0.125":         290,
        "aluminum|0.1875":        180,
        "aluminum|0.250":         120,
        "aluminum|0.375":          65,
        "aluminum|0.500":          35,
    },

    # ── Laser Capable Materials ───────────────────────────────────
    # Materials the laser can cut, and max thickness (in)
    "laser_capable": {
        "carbon_steel":    1.000,
        "stainless_steel": 1.000,
        "aluminum":        0.500,
    },

    # ── Water Jet Speeds (IPM) ────────────────────────────────────
    # key = "thickness_in", value = [standard, precision]
    "waterjet_speeds": {
        "0.250": [8.0, 4.0],
        "0.500": [4.0, 2.0],
        "1.000": [1.5, 0.75],
        "2.000": [0.6, 0.3],
    },

    # ── Machinability Index (mild steel = 1.0) ────────────────────
    "machinability": {
        "carbon_steel":    1.0,
        "stainless_steel": 0.9,
        "aluminum":        2.9,
        "copper":          2.0,
        "brass":           2.5,
        "titanium":        0.4,
        "wood":            5.0,
        "uhmw":            6.0,
        "acetal":          5.5,
    },

    # ── Press Brake ───────────────────────────────────────────────
    "bend_time_per_bend": {
        "brake_adira":  0.05,
        "brake_guifil": 0.06,
    },

    # ── Welding (hr/weld-inch) ────────────────────────────────────
    "weld_rates": {
        "tig":   0.01058,
        "mig":   0.00280,
        "laser": 0.00444,
    },

    # ── Production Ramp ───────────────────────────────────────────
    # [qty, ramp_factor]  (lower = more time per piece)
    "ramp_table": [
        [1,    0.50],
        [10,   0.65],
        [25,   0.80],
        [50,   0.90],
        [100,  1.00],
        [1000, 1.00],
    ],

    # ── Batch Handling ────────────────────────────────────────────
    "batch_threshold_hr": 6.0,
    "batch_handling": {
        "laser":        0.20,
        "water_jet":    0.20,
        "brake_adira":  0.15,
        "brake_guifil": 0.15,
        "tig_weld":     0.20,
    },

    # ── Second Operator Rules ─────────────────────────────────────
    "second_op_weight_lb": 50,
    "second_op_size_in":   60,

    # ── Material Density (lb/in^3) ────────────────────────────────
    "density": {
        "carbon_steel":    0.284,
        "stainless_steel": 0.289,
        "aluminum":        0.098,
        "copper":          0.323,
        "brass":           0.307,
        "titanium":        0.163,
    },

    # ── Raw Material Cost ($/lb) ──────────────────────────────────
    "material_cost_per_lb": {
        "carbon_steel":    1.10,
        "stainless_steel": 3.25,
        "aluminum":        3.50,
        "copper":          4.80,
        "brass":           3.20,
        "titanium":       12.00,
        "uhmw":            1.60,
        "acetal":          2.80,
        "wood":            0.30,
    },

    # ── Hardware Insertion ────────────────────────────────────────
    "hardware_time_per_insert": 0.015,
    "hardware_setup": 0.20,

    # ── Tapping ───────────────────────────────────────────────────
    "tap_time_per_hole": {
        "small":  0.008,
        "medium": 0.012,
        "large":  0.018,
    },
    "tap_setup": 0.20,

    # ── Countersinking ────────────────────────────────────────────
    "csink_time_per_hole": 0.010,
    "csink_setup": 0.15,

    # ── Passivation ───────────────────────────────────────────────
    "passivation_time_per_sqft": 0.04,
    "passivation_setup": 0.35,
    "passivation_min_charge": 0.25,
    "passivation_parts_per_batch": 50,

    # ── Sawing ────────────────────────────────────────────────────
    "saw_time_per_cut": {
        "carbon_steel":    0.05,
        "stainless_steel": 0.08,
        "aluminum":        0.03,
        "copper":          0.04,
        "brass":           0.04,
        "titanium":        0.12,
    },

    # ── CNC Material Removal Rates (in^3/min) ────────────────────
    "mrr_turning": {
        "carbon_steel":    1.8,
        "stainless_steel": 1.5,
        "aluminum":        4.0,
        "copper":          2.5,
        "brass":           3.0,
        "titanium":        0.6,
    },
    "mrr_milling": {
        "carbon_steel":    0.8,
        "stainless_steel": 0.6,
        "aluminum":        2.0,
        "copper":          1.2,
        "brass":           1.5,
        "titanium":        0.3,
    },

    # ── Deburring ─────────────────────────────────────────────────
    "deburr_apex_hr_per_sqft": 0.08,
    "deburr_grizzly_hr_per_sqft": 0.15,
    "deburr_hand_hr_per_part": 0.20,
    "deburr_apex_max_thickness": 0.5,
    "deburr_apex_max_width": 32,
    "deburr_apex_min_area_sqin": 50,

    # ── Packaging / Final Inspection ──────────────────────────────
    "packaging_time_per_part": 0.005,
    "packaging_setup": 0.15,
    "packaging_rate": 125.00,

    # ── Scrap Allowance ───────────────────────────────────────────
    "scrap_allowance_pct": 15,

    # ── Assist Gas Selection ─────────────────────────────────────
    # Gas type by material: O2 for carbon steel, N2 for stainless, air when eligible
    "assist_gas": {
        "carbon_steel":    "oxygen",
        "stainless_steel": "nitrogen",
        "aluminum":        "nitrogen",
    },
    # 22TK air cutting eligible: material + max thickness (in)
    "air_cut_eligible": {
        "carbon_steel":    0.1875,   # up to 3/16"
        "stainless_steel": 0.120,    # up to ~11ga
        "aluminum":        0.250,    # up to 1/4"
    },

    # ── Laser Burden Breakdown ($/hr by gas + thickness tier) ────
    "laser_burden": {
        "air":              {"gas": 1.00, "electricity": 5.00, "consumables": 2.00},
        "oxygen_thin":      {"gas": 2.00, "electricity": 5.00, "consumables": 2.00},
        "oxygen_medium":    {"gas": 5.00, "electricity": 5.00, "consumables": 2.50},
        "oxygen_thick":     {"gas": 9.00, "electricity": 6.36, "consumables": 3.00},
        "nitrogen_thin":    {"gas": 8.00, "electricity": 5.00, "consumables": 2.00},
        "nitrogen_medium":  {"gas": 20.00, "electricity": 5.00, "consumables": 2.50},
        "nitrogen_thick":   {"gas": 32.00, "electricity": 6.36, "consumables": 3.00},
    },
    "laser_depreciation": 30.00,
    "labor_rate": 125.00,

    # ── Water Jet Burden ─────────────────────────────────────────
    "waterjet_burden": {
        "electricity": 3.39,
        "abrasive_base": 16.20,    # garnet at $0.30/lb, ~54 lb/hr @ 60kpsi
        "consumables": 2.30,
    },
    "waterjet_abrasive_factor": {
        "carbon_steel": 1.0, "stainless_steel": 1.0, "aluminum": 1.0,
        "wood": 0.7, "uhmw": 0.5, "acetal": 0.6,
    },
    "waterjet_depreciation": 30.00,

    # ── Setup Time Itemization (hr) ──────────────────────────────
    "setup_detail": {
        "laser":        {"programming": 0.25, "material_pull": 0.10, "staging": 0.05, "accounting": 0.05, "qa": 0.10},
        "water_jet":    {"programming": 0.25, "material_pull": 0.10, "staging": 0.05, "accounting": 0.05, "qa": 0.10},
        "brake_adira":  {"programming": 0.25, "material_pull": 0.10, "staging": 0.05, "accounting": 0.05, "qa": 0.10},
        "brake_guifil": {"programming": 0.25, "material_pull": 0.10, "staging": 0.05, "accounting": 0.05, "qa": 0.10},
        "st30_turning": {"programming": 0.25, "material_pull": 0.10, "staging": 0.10, "accounting": 0.05, "qa": 0.15},
        "tm2p_milling": {"programming": 1.75, "material_pull": 0.10, "staging": 0.10, "accounting": 0.05, "qa": 0.15},
        "tig_weld":     {"programming": 0.05, "material_pull": 0.05, "staging": 0.05, "accounting": 0.05, "qa": 0.10},
    },

    # ── Welding Bench/Off-Bench Setup ────────────────────────────
    "weld_bench_max_length": 42,   # inches
    "weld_bench_max_width": 72,    # inches
    "weld_base_travel_setup": 0.15,  # hr - bringing welder to work
    "weld_off_bench_adder": 0.50,    # hr - if part exceeds bench
    "weld_fixture_setup": 0.50,      # hr - if fixture required

    # ── Brake Routing (Adira vs Guifil) ──────────────────────────
    "brake_adira_max_tonnage": 160,
    "brake_adira_bed_length": 157,  # inches (~4000mm)
    "brake_guifil_max_tonnage": 110,
    "brake_guifil_bed_length": 120, # inches (10ft)

    # ── Hole Quality Thresholds ──────────────────────────────────
    "hole_chase_gauge_threshold": 7,  # > 7 gauge needs drill press chasing

    # ── Machine Capacity ─────────────────────────────────────────
    "machine_capacity": {
        "laser_bed": [157, 79],     # inches (G4020X ~4m x 2m)
        "waterjet_bed": [126, 62],  # inches (OMAX 60120)
        "brake_adira_length": 157,
        "brake_guifil_length": 120,
        "st30_x": 12.5, "st30_z": 26.0,
        "tl2_x": 16.0, "tl2_z": 48.0,   # Haas TL-2: max cutting dia / length
        "tm2p_x": 16.0, "tm2p_y": 12.0, "tm2p_z": 16.0,
    },

    # ── Tight-tolerance cycle factor (machined parts) ─────────────
    # [max tolerance band (in), cycle-time multiplier] - first match wins
    "tolerance_cycle_factor": [[0.0005, 1.50], [0.001, 1.25], [0.002, 1.10]],

    # ── Markups & Minimums ────────────────────────────────────────
    "minimum_order_charge": 75.00,
    "rush_premium_pct": 50,
    "material_markup_pct": 15,
    "shop_markup_pct": 0,
}


def get_default_config():
    """Return a deep copy of the default config for seeding the database."""
    return copy.deepcopy(DEFAULT_CONFIG)


# ═══════════════════════════════════════════════════════════════════
#  Helper functions
# ═══════════════════════════════════════════════════════════════════

def _cfg(config, *keys):
    """Walk into a config dict by key path, falling back to DEFAULT_CONFIG."""
    obj = config or DEFAULT_CONFIG
    for k in keys:
        if isinstance(obj, dict) and k in obj:
            obj = obj[k]
        else:
            # fall back to default
            obj = DEFAULT_CONFIG
            for k2 in keys:
                obj = obj[k2]
            return obj
    return obj


def _normalize_material(material_str):
    """Map user-facing material names to internal keys."""
    m = (material_str or "").lower().strip()
    if "stainless" in m or "304" in m or "316" in m:
        return "stainless_steel"
    if "aluminum" in m or "aluminium" in m or "6061" in m or "5052" in m:
        return "aluminum"
    if "carbon" in m or "steel" in m or "1018" in m or "a36" in m or "4130" in m:
        return "carbon_steel"
    if "copper" in m:
        return "copper"
    if "brass" in m:
        return "brass"
    if "titanium" in m:
        return "titanium"
    if "uhmw" in m:
        return "uhmw"
    if "acetal" in m or "delrin" in m:
        return "acetal"
    if "wood" in m:
        return "wood"
    return "carbon_steel"


def _get_ramp_factor(qty, config=None):
    """Interpolate production ramp factor for a given quantity."""
    ramp_table = _cfg(config, "ramp_table")
    if qty <= 1:
        return ramp_table[0][1] if ramp_table else 0.50
    prev_q, prev_r = ramp_table[0]
    for q, r in ramp_table:
        if qty <= q:
            frac = (qty - prev_q) / max(q - prev_q, 1)
            return prev_r + frac * (r - prev_r)
        prev_q, prev_r = q, r
    return 1.0


def _tolerance_factor(band_in, config=None):
    """Cycle-time multiplier for tight tolerance bands (None -> 1.0)."""
    if not band_in:
        return 1.0
    try:
        for max_band, factor in _cfg(config, "tolerance_cycle_factor"):
            if band_in <= max_band + 1e-9:
                return float(factor)
    except Exception:
        pass
    return 1.0


def _turning_machine(dims, config=None):
    """Pick the lathe for a turned part: ST-30 if it fits, else TL-2.

    Returns (op_key, op_name, setup_multiplier, warnings).
    """
    cap = _cfg(config, "machine_capacity")
    vals = sorted([dims.get("length", 0) or 0, dims.get("width", 0) or 0, dims.get("height", 0) or 0])
    length, dia = vals[2], vals[1]
    st_x, st_z = cap.get("st30_x", 12.5), cap.get("st30_z", 26.0)
    tl_x, tl_z = cap.get("tl2_x", 16.0), cap.get("tl2_z", 48.0)
    if length <= st_z and dia <= st_x:
        return "st30_turning", "CNC Turning (Haas ST-30)", 1.0, []
    warns = []
    if length <= tl_z and dia <= tl_x:
        warns.append(f"Routed to TL-2: {length:.2f}\" x {dia:.2f}\" dia exceeds ST-30 "
                     f"({st_z}\" Z / {st_x}\" dia)")
        return "tl2_turning", "CNC Turning (Haas TL-2)", 1.0, warns
    warns.append(f"Part {length:.2f}\" long x {dia:.2f}\" dia exceeds both lathes "
                 f"(ST-30 {st_z}\", TL-2 {tl_z}\" max length) - priced on TL-2 with 2 setups "
                 f"(end-for-end); confirm with shop or quote outside")
    return "tl2_turning", "CNC Turning (Haas TL-2, end-for-end)", 2.0, warns


def _machined_route(geometry, dims, config=None):
    """Pick turning vs milling for a machined part. Returns (op_key, mrr_key)."""
    l, w, h = dims.get("length", 0), dims.get("width", 0), dims.get("height", 0)
    sd = sorted([l, w, h], reverse=True)
    aspect = sd[0] / max(sd[1], 0.01) if sd[0] > 0 and sd[1] > 0 else 1.0
    if geometry.get("turning_hint") or (aspect > 2.0 and max(sd[1], sd[2]) <= 12.5):
        return "st30_turning", "mrr_turning"
    return "tm2p_milling", "mrr_milling"


def _find_nearest(table_keys, value):
    """Find the nearest key in a list of numeric keys."""
    return min(table_keys, key=lambda k: abs(k - value))


def _calc_batches(run_time_hr, handling_hr_per_batch, threshold=6.0):
    """Calculate number of batches and extra handling time."""
    if run_time_hr <= threshold:
        return 1, 0.0
    n_batches = max(1, int(run_time_hr / threshold))
    extra = (n_batches - 1) * handling_hr_per_batch
    return n_batches, extra


def _laser_can_cut(material, thickness_in, config=None):
    """Check if laser can cut this material/thickness."""
    capable = _cfg(config, "laser_capable")
    max_t = capable.get(material)
    if max_t is None:
        return False
    return thickness_in <= max_t


def _get_laser_speed(material, thickness_in, config=None):
    """Get laser cutting speed in in/min for material and thickness."""
    speeds = _cfg(config, "laser_speeds")
    # Find all thicknesses for this material
    prefix = material + "|"
    mat_entries = {}
    for k, v in speeds.items():
        if k.startswith(prefix):
            t = float(k.split("|")[1])
            mat_entries[t] = v
    if not mat_entries:
        return None
    nearest = _find_nearest(list(mat_entries.keys()), thickness_in)
    speed = mat_entries[nearest]
    if speed and nearest != thickness_in and nearest > 0:
        ratio = nearest / thickness_in
        if ratio > 1:
            speed = speed * min(ratio, 1.5)
        else:
            speed = speed * max(ratio, 0.5)
    return speed


def _get_assist_gas(material, thickness_in, config=None):
    """Determine which assist gas the laser uses and its burden tier."""
    C = config or DEFAULT_CONFIG
    # Check air eligibility first (cheapest option)
    air_eligible = _cfg(C, "air_cut_eligible")
    max_air = air_eligible.get(material)
    if max_air is not None and thickness_in <= max_air:
        return "air", "Compressed Air (22TK)"

    # Otherwise use material-specific gas
    gas_map = _cfg(C, "assist_gas")
    gas = gas_map.get(material, "nitrogen")

    if gas == "oxygen":
        display = "Oxygen (O2)"
    else:
        display = "Nitrogen (N2)"
    return gas, display


def _get_laser_burden_detail(gas_type, thickness_in, config=None):
    """Get itemized laser burden (gas + electricity + consumables) by gas and thickness tier."""
    C = config or DEFAULT_CONFIG
    burden_table = _cfg(C, "laser_burden")

    if gas_type == "air":
        key = "air"
    else:
        # Determine thickness tier
        if thickness_in <= 0.1875:
            tier = "thin"
        elif thickness_in <= 0.500:
            tier = "medium"
        else:
            tier = "thick"
        key = f"{gas_type}_{tier}"

    burden = burden_table.get(key, burden_table.get("air", {"gas": 1, "electricity": 5, "consumables": 2}))
    depreciation = _cfg(C, "laser_depreciation")
    labor = _cfg(C, "labor_rate")

    total = labor + burden["gas"] + burden["electricity"] + burden["consumables"] + depreciation
    return {
        "labor": labor,
        "gas": burden["gas"],
        "electricity": burden["electricity"],
        "consumables": burden["consumables"],
        "depreciation": depreciation,
        "total": round(total, 2),
    }


def _get_waterjet_burden_detail(material, config=None):
    """Get itemized water jet burden."""
    C = config or DEFAULT_CONFIG
    burden = _cfg(C, "waterjet_burden")
    abrasive_factors = _cfg(C, "waterjet_abrasive_factor")
    factor = abrasive_factors.get(material, 1.0)
    labor = _cfg(C, "labor_rate")
    depreciation = _cfg(C, "waterjet_depreciation")

    abrasive_cost = burden["abrasive_base"] * factor
    total = labor + burden["electricity"] + abrasive_cost + burden["consumables"] + depreciation
    return {
        "labor": labor,
        "electricity": burden["electricity"],
        "abrasive": round(abrasive_cost, 2),
        "consumables": burden["consumables"],
        "depreciation": depreciation,
        "total": round(total, 2),
    }


def _select_brake(thickness_in, flat_width_in, weight_lb, config=None):
    """Route to Adira (160T) or Guifil (110T) press brake."""
    C = config or DEFAULT_CONFIG
    # Rough tonnage estimate: ~8 tons per foot of bend length per 0.1" mild steel
    bend_length_ft = flat_width_in / 12.0 if flat_width_in > 0 else 4.0
    tonnage_est = bend_length_ft * (thickness_in / 0.1) * 8.0

    adira_max = _cfg(C, "brake_adira_max_tonnage")
    adira_bed = _cfg(C, "brake_adira_bed_length")
    guifil_max = _cfg(C, "brake_guifil_max_tonnage")
    guifil_bed = _cfg(C, "brake_guifil_bed_length")

    # If part fits Guifil (lighter duty, lower rate), use it for cost savings
    if tonnage_est <= guifil_max and flat_width_in <= guifil_bed:
        return "brake_guifil", "Press Brake Form (Guifil 110T)", round(tonnage_est, 0)
    elif tonnage_est <= adira_max and flat_width_in <= adira_bed:
        return "brake_adira", "Press Brake Form (Adira 160T)", round(tonnage_est, 0)
    else:
        return "brake_adira", "Press Brake Form (Adira 160T - NEAR CAPACITY)", round(tonnage_est, 0)


def _get_weld_setup(part_length_in, part_width_in, needs_fixture=False, config=None):
    """Calculate welding setup time based on bench/off-bench/fixture rules."""
    C = config or DEFAULT_CONFIG
    base = _cfg(C, "weld_base_travel_setup")
    bench_max_l = _cfg(C, "weld_bench_max_length")
    bench_max_w = _cfg(C, "weld_bench_max_width")

    off_bench = part_length_in > bench_max_l or part_width_in > bench_max_w
    off_bench_adder = _cfg(C, "weld_off_bench_adder") if off_bench else 0.0

    fixture_adder = _cfg(C, "weld_fixture_setup") if needs_fixture else 0.0

    location = "Off-Bench" if off_bench else "Bench"
    total = base + off_bench_adder + fixture_adder
    return {
        "base": base,
        "off_bench_adder": off_bench_adder,
        "fixture_adder": fixture_adder,
        "total": round(total, 2),
        "location": location,
    }


def _check_hole_quality(features, gauge_num, config=None):
    """Flag holes that need drill press chasing based on gauge threshold."""
    C = config or DEFAULT_CONFIG
    threshold = _cfg(C, "hole_chase_gauge_threshold")
    warnings = []
    chase_count = 0

    if gauge_num is not None and gauge_num > threshold:
        for f in (features or []):
            if f.get("type") == "round":
                chase_count += 1
        if chase_count > 0:
            warnings.append(
                f"Material is {gauge_num} gauge (> {threshold} ga): "
                f"{chase_count} round hole(s) may need drill press chasing for precision diameters"
            )
    return warnings, chase_count


def _check_machine_capacity(fab_type, dims, flat_w, flat_l, config=None):
    """Validate part fits within machine bed dimensions."""
    C = config or DEFAULT_CONFIG
    cap = _cfg(C, "machine_capacity")
    warnings = []

    if "sheet" in fab_type.lower():
        # Check laser bed
        laser_bed = cap.get("laser_bed", [157, 79])
        if flat_w > laser_bed[0] or flat_l > laser_bed[1]:
            if flat_w > laser_bed[1] or flat_l > laser_bed[0]:  # try rotated
                warnings.append(
                    f"Part flat size ({flat_w:.1f}\" x {flat_l:.1f}\") exceeds laser bed "
                    f"({laser_bed[0]}\" x {laser_bed[1]}\")"
                )
    return warnings


def _get_setup_detail(op_key, config=None):
    """Get itemized setup time breakdown for an operation."""
    C = config or DEFAULT_CONFIG
    details = _cfg(C, "setup_detail")
    if op_key in details:
        d = details[op_key]
        items = []
        labels = {"programming": "Programming", "material_pull": "Material Pull",
                  "staging": "Staging", "accounting": "Accounting", "qa": "QA"}
        for k, v in d.items():
            if v > 0:
                items.append({"key": k, "label": labels.get(k, k.title()), "hr": v})
        return {
            "programming": d.get("programming", 0),
            "material_pull": d.get("material_pull", 0),
            "staging": d.get("staging", 0),
            "accounting": d.get("accounting", 0),
            "qa": d.get("qa", 0),
            "total": round(sum(d.values()), 2),
            "items": items,
        }
    return None


def _get_waterjet_speed(material, thickness_in, precision=False, config=None):
    """Get water jet cutting speed in in/min."""
    wj = _cfg(config, "waterjet_speeds")
    thicknesses = [float(k) for k in wj.keys()]
    nearest = _find_nearest(thicknesses, thickness_in)
    entry = wj[str(nearest)] if str(nearest) in wj else wj.get(f"{nearest:.3f}", [4.0, 2.0])
    if isinstance(entry, list):
        std, prec = entry[0], entry[1]
    else:
        std, prec = entry, entry * 0.5
    base = prec if precision else std
    mac = _cfg(config, "machinability").get(material, 1.0)
    return base * mac


# ═══════════════════════════════════════════════════════════════════
#  Main estimation function
# ═══════════════════════════════════════════════════════════════════

def estimate_cost(geometry, material_str, quantity=1, config=None):
    """
    Estimate fabrication cost from extracted geometry.

    Args:
        geometry: dict with keys from STEP or PDF extraction
        material_str: user-selected material string
        quantity: int, number of parts
        config: optional config dict (from admin panel / SQLite).
                If None, uses DEFAULT_CONFIG.

    Returns:
        dict with cost breakdown
    """
    C = config or DEFAULT_CONFIG
    material = _normalize_material(material_str)
    fab_type = (geometry.get("fab_type") or "Sheet Metal").lower()
    thickness = geometry.get("thickness_in", 0.0) or 0.0
    dims = geometry.get("dims", {})
    max_dim = max(dims.get("length", 0), dims.get("width", 0), dims.get("height", 0))
    weight = geometry.get("weight_lb", 0.0) or 0.0
    bend_count = geometry.get("bend_count", 0) or 0
    cut_perim = geometry.get("cut_perimeter_in", 0.0) or 0.0
    weld_length = geometry.get("weld_length_in", 0.0) or 0.0

    rates = _cfg(C, "rates")
    setup_times = _cfg(C, "setup")

    ramp = _get_ramp_factor(quantity, C)
    operations = []
    warnings = []
    gauge_num = geometry.get("gauge_num", None)
    features = geometry.get("features_list", [])

    # ── Sheet Metal Path ────────────────────────────────────────
    flat_l = geometry.get("flat_length_in", 0) or 0
    flat_w = geometry.get("flat_width_in", 0) or 0

    if "sheet" in fab_type or "sheet metal" in fab_type:

        # Machine capacity check
        cap_warnings = _check_machine_capacity(fab_type, dims, flat_w, flat_l, C)
        warnings.extend(cap_warnings)

        # Hole quality check
        hq_warnings, chase_count = _check_hole_quality(features, gauge_num, C)
        warnings.extend(hq_warnings)

        use_laser = _laser_can_cut(material, thickness, C)

        if use_laser:
            speed = _get_laser_speed(material, thickness, C)
            if speed and cut_perim > 0:
                cut_time_hr = (cut_perim / speed) / 60.0
            else:
                cut_time_hr = 0.35
            op_key = "laser"
            op_name = "Laser Cut (HSG G4020X 12kW)"

            # Assist gas selection and burden breakdown
            gas_type, gas_display = _get_assist_gas(material, thickness, C)
            burden_detail = _get_laser_burden_detail(gas_type, thickness, C)
            rate = burden_detail["total"]
        else:
            speed = _get_waterjet_speed(material, thickness, config=C)
            if speed and cut_perim > 0:
                cut_time_hr = (cut_perim / speed) / 60.0
            else:
                cut_time_hr = 0.60
            op_key = "water_jet"
            op_name = "Water Jet Cut (OMAX 60120)"
            gas_display = None
            gas_type = None

            # Water jet burden breakdown
            burden_detail = _get_waterjet_burden_detail(material, C)
            rate = burden_detail["total"]

        setup_detail = _get_setup_detail(op_key, C)
        setup = setup_detail["total"] if setup_detail else setup_times.get(op_key, 0.55)
        run_time = cut_time_hr / ramp * quantity
        batch_threshold = _cfg(C, "batch_threshold_hr")
        batch_handling = _cfg(C, "batch_handling")
        n_batch, batch_extra = _calc_batches(run_time, batch_handling.get(op_key, 0.15), batch_threshold)
        total_time = setup + run_time + batch_extra
        total_cost = total_time * rate

        op_data = {
            "operation": op_name,
            "op_key": op_key,
            "speed_ipm": round(speed, 1) if speed else None,
            "cycle_time_hr": round(cut_time_hr, 4),
            "setup_hr": round(setup, 2),
            "setup_detail": setup_detail,
            "run_time_hr": round(run_time, 4),
            "batches": n_batch,
            "batch_handling_hr": round(batch_extra, 2),
            "total_time_hr": round(total_time, 4),
            "rate_per_hr": rate,
            "burden_detail": burden_detail,
            "total_cost": round(total_cost, 2),
        }
        if gas_display:
            op_data["assist_gas"] = gas_display
        operations.append(op_data)

        # 2. Bending
        if bend_count > 0:
            # Smart brake routing based on tonnage and bed length
            brake_key, brake_name, tonnage_est = _select_brake(thickness, flat_w, weight, C)
            bend_times = _cfg(C, "bend_time_per_bend")
            time_per_bend = bend_times.get(brake_key, 0.05)
            bend_cycle = time_per_bend * bend_count
            bend_run = bend_cycle / ramp * quantity
            bend_setup_detail = _get_setup_detail(brake_key, C)
            bend_setup = bend_setup_detail["total"] if bend_setup_detail else setup_times.get(brake_key, 0.55)
            n_b, b_extra = _calc_batches(bend_run, batch_handling.get(brake_key, 0.15), batch_threshold)
            bend_total_time = bend_setup + bend_run + b_extra
            bend_rate = rates.get(brake_key, 157.0)
            bend_cost = bend_total_time * bend_rate

            second_op_weight = _cfg(C, "second_op_weight_lb")
            second_op_size = _cfg(C, "second_op_size_in")
            needs_2nd_op = weight > second_op_weight or max_dim > second_op_size
            if needs_2nd_op:
                labor_rate = rates.get("labor_only", 125.0)
                second_op_cost = bend_run * labor_rate
                bend_cost += second_op_cost
                warnings.append(f"Second operator added for bending (weight {weight:.1f} lb or size {max_dim:.1f} in)")
            else:
                second_op_cost = 0.0

            operations.append({
                "operation": brake_name,
                "op_key": brake_key,
                "bends": bend_count,
                "tonnage_est": tonnage_est,
                "time_per_bend": time_per_bend,
                "cycle_time_hr": round(bend_cycle, 4),
                "setup_hr": round(bend_setup, 2),
                "setup_detail": bend_setup_detail,
                "run_time_hr": round(bend_run, 4),
                "batches": n_b,
                "batch_handling_hr": round(b_extra, 2),
                "total_time_hr": round(bend_total_time, 4),
                "rate_per_hr": bend_rate,
                "second_operator": needs_2nd_op,
                "second_op_cost": round(second_op_cost, 2),
                "total_cost": round(bend_cost, 2),
            })

        # 3. Deburring (3-tier: Apex Time Saver > Grizzly Flap Wheel > Hand)
        if flat_l > 0 and flat_w > 0:
            area_sqft = (flat_l * flat_w) / 144.0
            area_sqin = flat_l * flat_w
            apex_max_t = _cfg(C, "deburr_apex_max_thickness")
            apex_max_w = _cfg(C, "deburr_apex_max_width")
            apex_min_area = _cfg(C, "deburr_apex_min_area_sqin")

            if (material == "stainless_steel" and thickness <= apex_max_t
                    and flat_w <= apex_max_w and area_sqin >= apex_min_area):
                # Apex Time Saver: 304 SS only, <= 1/2", <= 32" wide, >= 50 sq in
                deburr_key = "deburr_apex"
                deburr_name = "Apex Time Saver (304 SS)"
                deburr_cycle = _cfg(C, "deburr_apex_hr_per_sqft") * area_sqft
            elif area_sqft >= 0.5:
                # Grizzly 36x36 manual flap wheel: for larger flat parts
                deburr_key = "deburr_manual"
                deburr_name = "Grizzly Flap Wheel Deburr"
                deburr_cycle = _cfg(C, "deburr_grizzly_hr_per_sqft") * area_sqft
            else:
                # Hand deburr: small parts
                deburr_key = "deburr_hand"
                deburr_name = "Hand Deburr"
                deburr_cycle = _cfg(C, "deburr_hand_hr_per_part")

            deburr_run = deburr_cycle / ramp * quantity
            deburr_setup = setup_times.get(deburr_key, 0.25)
            deburr_total = deburr_setup + deburr_run
            deburr_rate = rates.get(deburr_key, 125.0)
            deburr_cost = deburr_total * deburr_rate

            operations.append({
                "operation": deburr_name,
                "op_key": deburr_key,
                "area_sqft": round(area_sqft, 3),
                "cycle_time_hr": round(deburr_cycle, 4),
                "setup_hr": round(deburr_setup, 2),
                "run_time_hr": round(deburr_run, 4),
                "total_time_hr": round(deburr_total, 4),
                "rate_per_hr": deburr_rate,
                "total_cost": round(deburr_cost, 2),
            })

        # 4. Hardware insertion
        hardware_count = geometry.get("hardware_count", 0) or 0
        if hardware_count > 0:
            hw_cycle = _cfg(C, "hardware_time_per_insert") * hardware_count
            hw_run = hw_cycle / ramp * quantity
            hw_setup = _cfg(C, "hardware_setup")
            hw_total = hw_setup + hw_run
            hw_rate = rates.get("labor_only", 125.0)
            hw_cost = hw_total * hw_rate

            operations.append({
                "operation": "Hardware Insertion (PEM)",
                "op_key": "hardware",
                "count": hardware_count,
                "cycle_time_hr": round(hw_cycle, 4),
                "setup_hr": round(hw_setup, 2),
                "run_time_hr": round(hw_run, 4),
                "total_time_hr": round(hw_total, 4),
                "rate_per_hr": hw_rate,
                "total_cost": round(hw_cost, 2),
            })

        # 5. Tapping
        tap_count = geometry.get("tap_count", 0) or 0
        if tap_count > 0:
            tap_size = geometry.get("tap_size_class", "medium")
            tap_times = _cfg(C, "tap_time_per_hole")
            tap_cycle = tap_times.get(tap_size, 0.012) * tap_count
            tap_run = tap_cycle / ramp * quantity
            tap_setup = _cfg(C, "tap_setup")
            tap_total = tap_setup + tap_run
            tap_rate = rates.get("labor_only", 125.0)
            tap_cost = tap_total * tap_rate

            operations.append({
                "operation": f"Tapping ({tap_count} holes)",
                "op_key": "tapping",
                "count": tap_count,
                "cycle_time_hr": round(tap_cycle, 4),
                "setup_hr": round(tap_setup, 2),
                "run_time_hr": round(tap_run, 4),
                "total_time_hr": round(tap_total, 4),
                "rate_per_hr": tap_rate,
                "total_cost": round(tap_cost, 2),
            })

        # 6. Countersinking
        csink_count = geometry.get("csink_count", 0) or 0
        if csink_count > 0:
            cs_cycle = _cfg(C, "csink_time_per_hole") * csink_count
            cs_run = cs_cycle / ramp * quantity
            cs_setup = _cfg(C, "csink_setup")
            cs_total = cs_setup + cs_run
            cs_rate = rates.get("labor_only", 125.0)
            cs_cost = cs_total * cs_rate

            operations.append({
                "operation": f"Countersink ({csink_count} holes)",
                "op_key": "countersink",
                "count": csink_count,
                "cycle_time_hr": round(cs_cycle, 4),
                "setup_hr": round(cs_setup, 2),
                "run_time_hr": round(cs_run, 4),
                "total_time_hr": round(cs_total, 4),
                "rate_per_hr": cs_rate,
                "total_cost": round(cs_cost, 2),
            })

        # 7. Passivation (stainless steel only)
        if material == "stainless_steel":
            part_area_sqft = (flat_l * flat_w * 2) / 144.0 if flat_l > 0 and flat_w > 0 else 0.5
            pass_rate_sqft = _cfg(C, "passivation_time_per_sqft")
            pass_min = _cfg(C, "passivation_min_charge")
            pass_cycle = max(pass_rate_sqft * part_area_sqft, pass_min)
            parts_per_batch = _cfg(C, "passivation_parts_per_batch")
            n_pass_batches = max(1, -(-quantity // parts_per_batch))
            pass_run = pass_cycle * n_pass_batches
            pass_setup = _cfg(C, "passivation_setup")
            pass_total = pass_setup + pass_run
            pass_rate = rates.get("passivation", 125.0)
            pass_cost = pass_total * pass_rate

            operations.append({
                "operation": "Passivation (Citric Acid)",
                "op_key": "passivation",
                "area_sqft": round(part_area_sqft, 3),
                "cycle_time_hr": round(pass_cycle, 4),
                "setup_hr": round(pass_setup, 2),
                "run_time_hr": round(pass_run, 4),
                "total_time_hr": round(pass_total, 4),
                "rate_per_hr": pass_rate,
                "total_cost": round(pass_cost, 2),
            })

    # ── Machined Part Path ──────────────────────────────────────
    elif "machin" in fab_type:
        volume_removed = geometry.get("volume_in3", 0.0) or 0.0

        # 1. Sawing
        saw_times = _cfg(C, "saw_time_per_cut")
        saw_time = saw_times.get(material, 0.05)
        saw_run = saw_time / ramp * quantity
        saw_setup = setup_times.get("sawing", 0.30)
        saw_total = saw_setup + saw_run
        saw_rate = rates.get("sawing", 125.0)
        saw_cost = saw_total * saw_rate

        operations.append({
            "operation": "Band Saw (Bar Stock Cutoff)",
            "op_key": "sawing",
            "cycle_time_hr": round(saw_time, 4),
            "setup_hr": round(saw_setup, 2),
            "run_time_hr": round(saw_run, 4),
            "total_time_hr": round(saw_total, 4),
            "rate_per_hr": saw_rate,
            "total_cost": round(saw_cost, 2),
        })

        # 2. CNC operation
        op_key, mrr_key = _machined_route(geometry, dims, C)
        op_name = "CNC Turning (Haas ST-30)" if op_key == "st30_turning" else "CNC Milling (Haas TM-2P)"
        mrr = _cfg(C, mrr_key).get(material, 1.8 if op_key == "st30_turning" else 0.8)
        setup_mult = 1.0
        if op_key == "st30_turning":
            op_key, op_name, setup_mult, lathe_warns = _turning_machine(dims, C)
            warnings.extend(lathe_warns)

        if volume_removed > 0 and mrr > 0:
            cycle = (volume_removed / mrr) / 60.0
        else:
            cycle = 0.25

        # Tight tolerances slow the finishing passes down
        tol_band = geometry.get("tightest_tolerance_in")
        tol_factor = _tolerance_factor(tol_band, C)
        if tol_factor > 1.0:
            cycle *= tol_factor
            warnings.append(f"Tight tolerance band {tol_band}\" - turning cycle x{tol_factor:.2f}"
                            + (" (grinding may be required)" if tol_band <= 0.0005 else ""))


        setup = setup_times.get(op_key, 0.65) * setup_mult
        rate = rates.get(op_key, 150.0)
        run_time = cycle / ramp * quantity
        total_time = setup + run_time
        total_cost = total_time * rate

        operations.append({
            "operation": op_name,
            "op_key": op_key,
            "cycle_time_hr": round(cycle, 4),
            "setup_hr": round(setup, 2),
            "run_time_hr": round(run_time, 4),
            "total_time_hr": round(total_time, 4),
            "rate_per_hr": rate,
            "total_cost": round(total_cost, 2),
        })

    # ── Welding (bench vs off-bench routing) ─────────────────────
    if weld_length > 0:
        weld_method = "tig"
        weld_key = "tig_weld"
        weld_rates_cfg = _cfg(C, "weld_rates")
        weld_cycle = weld_rates_cfg.get(weld_method, 0.01058) * weld_length
        weld_run = weld_cycle / ramp * quantity

        # Bench/off-bench setup logic using part dimensions
        part_length = max(dims.get("length", 0), flat_l)
        part_width = max(dims.get("width", 0), flat_w)
        needs_fixture = geometry.get("needs_weld_fixture", False)
        weld_setup_detail = _get_weld_setup(part_length, part_width, needs_fixture, C)
        weld_setup = weld_setup_detail["total"]
        weld_location = weld_setup_detail["location"]
        weld_name = f"TIG Weld ({weld_location})"

        weld_total = weld_setup + weld_run
        weld_rate = rates.get(weld_key, 131.0)
        weld_cost = weld_total * weld_rate

        if weld_location == "Off-Bench":
            warnings.append(
                f"Welding routed off-bench (part exceeds 42\" x 72\" bench): "
                f"+{weld_setup_detail['off_bench_adder']:.2f} hr setup"
            )

        operations.append({
            "operation": weld_name,
            "op_key": weld_key,
            "weld_length_in": weld_length,
            "weld_location": weld_location,
            "weld_setup_detail": weld_setup_detail,
            "cycle_time_hr": round(weld_cycle, 4),
            "setup_hr": round(weld_setup, 2),
            "run_time_hr": round(weld_run, 4),
            "total_time_hr": round(weld_total, 4),
            "rate_per_hr": weld_rate,
            "total_cost": round(weld_cost, 2),
        })

    for note in geometry.get("cost_assumptions", []) or []:
        warnings.append("Assumption: " + note)

    # ── Material Cost ──────────────────────────────────────────
    mat_costs = _cfg(C, "material_cost_per_lb")
    mat_cost_per_lb = mat_costs.get(material, 0.50)
    scrap_pct = _cfg(C, "scrap_allowance_pct")
    if weight > 0:
        mat_weight = weight * (1 + scrap_pct / 100.0)
        mat_cost_total = mat_cost_per_lb * mat_weight * quantity
        operations.append({
            "operation": "Raw Material",
            "op_key": "material",
            "weight_lb": round(weight, 3),
            "scrap_allowance": f"{scrap_pct}%",
            "cost_per_lb": mat_cost_per_lb,
            "cycle_time_hr": 0,
            "setup_hr": 0,
            "run_time_hr": 0,
            "total_time_hr": 0,
            "rate_per_hr": 0,
            "total_cost": round(mat_cost_total, 2),
        })

    # ── Packaging / Final Inspection ───────────────────────────
    pkg_cycle = _cfg(C, "packaging_time_per_part")
    pkg_run = pkg_cycle * quantity
    pkg_setup = _cfg(C, "packaging_setup")
    pkg_total = pkg_setup + pkg_run
    pkg_rate = _cfg(C, "packaging_rate")
    pkg_cost = pkg_total * pkg_rate

    operations.append({
        "operation": "Packaging & Inspection",
        "op_key": "packaging",
        "cycle_time_hr": round(pkg_cycle, 4),
        "setup_hr": round(pkg_setup, 2),
        "run_time_hr": round(pkg_run, 4),
        "total_time_hr": round(pkg_total, 4),
        "rate_per_hr": pkg_rate,
        "total_cost": round(pkg_cost, 2),
    })

    # ── Totals ──────────────────────────────────────────────────
    total_cost = sum(op["total_cost"] for op in operations)
    total_time = sum(op["total_time_hr"] for op in operations)
    unit_cost = total_cost / max(quantity, 1)

    # ── Quantity Breaks ─────────────────────────────────────────
    qty_breaks = []
    for qty in [1, 10, 25, 50, 100, 500, 1000]:
        if qty == quantity:
            qty_breaks.append({
                "qty": qty,
                "total_cost": round(total_cost, 2),
                "unit_cost": round(unit_cost, 2),
                "selected": True,
            })
        else:
            qb = estimate_cost_simple(geometry, material_str, qty, config=C)
            qty_breaks.append({
                "qty": qty,
                "total_cost": round(qb["total_cost"], 2),
                "unit_cost": round(qb["unit_cost"], 2),
                "selected": False,
            })

    return {
        "material": material,
        "material_display": material_str,
        "quantity": quantity,
        "ramp_factor": round(ramp, 2),
        "fab_type": geometry.get("fab_type", "Sheet Metal"),
        "operations": operations,
        "total_time_hr": round(total_time, 4),
        "total_cost": round(total_cost, 2),
        "unit_cost": round(unit_cost, 2),
        "warnings": warnings,
        "qty_breaks": qty_breaks,
    }


def estimate_cost_simple(geometry, material_str, quantity=1, config=None):
    """Simplified cost calc for quantity break table (no recursion).
    Uses the same burden-based rates and routing as estimate_cost()."""
    C = config or DEFAULT_CONFIG
    material = _normalize_material(material_str)
    fab_type = (geometry.get("fab_type") or "Sheet Metal").lower()
    thickness = geometry.get("thickness_in", 0.0) or 0.0
    dims = geometry.get("dims", {})
    max_dim = max(dims.get("length", 0), dims.get("width", 0), dims.get("height", 0))
    weight = geometry.get("weight_lb", 0.0) or 0.0
    bend_count = geometry.get("bend_count", 0) or 0
    cut_perim = geometry.get("cut_perimeter_in", 0.0) or 0.0
    weld_length = geometry.get("weld_length_in", 0.0) or 0.0
    flat_l = geometry.get("flat_length_in", 0) or 0
    flat_w = geometry.get("flat_width_in", 0) or 0

    rates = _cfg(C, "rates")
    setup_times = _cfg(C, "setup")
    ramp = _get_ramp_factor(quantity, C)
    total_cost = 0.0
    total_time = 0.0
    batch_threshold = _cfg(C, "batch_threshold_hr")
    batch_handling = _cfg(C, "batch_handling")

    if "sheet" in fab_type or "sheet metal" in fab_type:
        use_laser = _laser_can_cut(material, thickness, C)
        if use_laser:
            speed = _get_laser_speed(material, thickness, C)
            cut_time = (cut_perim / speed / 60.0) if speed and cut_perim > 0 else 0.35
            op_key = "laser"
            gas_type, _ = _get_assist_gas(material, thickness, C)
            burden = _get_laser_burden_detail(gas_type, thickness, C)
            rate = burden["total"]
        else:
            speed = _get_waterjet_speed(material, thickness, config=C)
            cut_time = (cut_perim / speed / 60.0) if speed and cut_perim > 0 else 0.60
            op_key = "water_jet"
            burden = _get_waterjet_burden_detail(material, C)
            rate = burden["total"]

        setup_d = _get_setup_detail(op_key, C)
        setup = setup_d["total"] if setup_d else setup_times.get(op_key, 0.55)
        run = cut_time / ramp * quantity
        _, b_extra = _calc_batches(run, batch_handling.get(op_key, 0.15), batch_threshold)
        t = setup + run + b_extra
        total_time += t
        total_cost += t * rate

        if bend_count > 0:
            bk, _, _ = _select_brake(thickness, flat_w, weight, C)
            bend_times = _cfg(C, "bend_time_per_bend")
            bc = bend_times.get(bk, 0.05) * bend_count
            br = bc / ramp * quantity
            _, be = _calc_batches(br, batch_handling.get(bk, 0.15), batch_threshold)
            bsetup_d = _get_setup_detail(bk, C)
            bsetup = bsetup_d["total"] if bsetup_d else setup_times.get(bk, 0.55)
            bt = bsetup + br + be
            total_time += bt
            total_cost += bt * rates.get(bk, 157.0)
            second_op_weight = _cfg(C, "second_op_weight_lb")
            second_op_size = _cfg(C, "second_op_size_in")
            if weight > second_op_weight or max_dim > second_op_size:
                total_cost += br * rates.get("labor_only", 125.0)

        # 3-tier deburr (matches estimate_cost)
        if flat_l > 0 and flat_w > 0:
            area_sqft = (flat_l * flat_w) / 144.0
            area_sqin = flat_l * flat_w
            apex_max_t = _cfg(C, "deburr_apex_max_thickness")
            apex_max_w = _cfg(C, "deburr_apex_max_width")
            apex_min_area = _cfg(C, "deburr_apex_min_area_sqin")
            if (material == "stainless_steel" and thickness <= apex_max_t
                    and flat_w <= apex_max_w and area_sqin >= apex_min_area):
                dc = _cfg(C, "deburr_apex_hr_per_sqft") * area_sqft
                dk = "deburr_apex"
            elif area_sqft >= 0.5:
                dc = _cfg(C, "deburr_grizzly_hr_per_sqft") * area_sqft
                dk = "deburr_manual"
            else:
                dc = _cfg(C, "deburr_hand_hr_per_part")
                dk = "deburr_hand"
            dr = dc / ramp * quantity
            dt = setup_times.get(dk, 0.25) + dr
            total_time += dt
            total_cost += dt * rates.get(dk, 125.0)

        hw = geometry.get("hardware_count", 0) or 0
        if hw > 0:
            hr_ = _cfg(C, "hardware_time_per_insert") * hw / ramp * quantity
            ht = _cfg(C, "hardware_setup") + hr_
            total_time += ht
            total_cost += ht * rates.get("labor_only", 125.0)

        tc = geometry.get("tap_count", 0) or 0
        if tc > 0:
            tap_times = _cfg(C, "tap_time_per_hole")
            tr_ = tap_times.get("medium", 0.012) * tc / ramp * quantity
            tt = _cfg(C, "tap_setup") + tr_
            total_time += tt
            total_cost += tt * rates.get("labor_only", 125.0)

        cc = geometry.get("csink_count", 0) or 0
        if cc > 0:
            cr_ = _cfg(C, "csink_time_per_hole") * cc / ramp * quantity
            ct = _cfg(C, "csink_setup") + cr_
            total_time += ct
            total_cost += ct * rates.get("labor_only", 125.0)

        if material == "stainless_steel":
            pass_rate_sqft = _cfg(C, "passivation_time_per_sqft")
            pass_min = _cfg(C, "passivation_min_charge")
            pa = max(pass_rate_sqft * 0.5, pass_min)
            ppb = _cfg(C, "passivation_parts_per_batch")
            n_pb = max(1, -(-quantity // ppb))
            pr_ = pa * n_pb
            pt = _cfg(C, "passivation_setup") + pr_
            total_time += pt
            total_cost += pt * rates.get("passivation", 125.0)

    elif "machin" in fab_type:
        saw_times = _cfg(C, "saw_time_per_cut")
        st_ = saw_times.get(material, 0.05)
        sr_ = st_ / ramp * quantity
        s_total = setup_times.get("sawing", 0.30) + sr_
        total_time += s_total
        total_cost += s_total * rates.get("sawing", 125.0)

        volume = geometry.get("volume_in3", 0.0) or 0.0
        op_key, mrr_key = _machined_route(geometry, dims, C)
        mrr = _cfg(C, mrr_key).get(material, 1.8 if op_key == "st30_turning" else 0.8)
        setup_mult = 1.0
        if op_key == "st30_turning":
            op_key, _nm, setup_mult, _w = _turning_machine(dims, C)

        cycle = max((volume / mrr / 60.0), 0.25) if volume > 0 and mrr > 0 else 0.25
        cycle *= _tolerance_factor(geometry.get("tightest_tolerance_in"), C)
        run = cycle / ramp * quantity
        t = setup_times.get(op_key, 0.65) * setup_mult + run
        total_time += t
        total_cost += t * rates.get(op_key, 150.0)

    # Welding with bench/off-bench setup
    if weld_length > 0:
        weld_rates_cfg = _cfg(C, "weld_rates")
        wc = weld_rates_cfg.get("tig", 0.01058) * weld_length
        wr = wc / ramp * quantity
        part_length = max(dims.get("length", 0), flat_l)
        part_width = max(dims.get("width", 0), flat_w)
        needs_fixture = geometry.get("needs_weld_fixture", False)
        ws_detail = _get_weld_setup(part_length, part_width, needs_fixture, C)
        wt = ws_detail["total"] + wr
        total_time += wt
        total_cost += wt * rates.get("tig_weld", 131.0)

    mat_costs = _cfg(C, "material_cost_per_lb")
    mat_cpl = mat_costs.get(material, 0.50)
    scrap_pct = _cfg(C, "scrap_allowance_pct")
    if weight > 0:
        total_cost += mat_cpl * weight * (1 + scrap_pct / 100.0) * quantity

    pkg_rate = _cfg(C, "packaging_rate")
    pkg_r = _cfg(C, "packaging_time_per_part") * quantity
    pkg_t = _cfg(C, "packaging_setup") + pkg_r
    total_time += pkg_t
    total_cost += pkg_t * pkg_rate

    unit_cost = total_cost / max(quantity, 1)
    return {"total_cost": round(total_cost, 2), "unit_cost": round(unit_cost, 2), "total_time_hr": round(total_time, 4)}
