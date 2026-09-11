"""
CMC Shop Cost Engine v2.0
Estimates fabrication cost from extracted geometry.

All rates/speeds/times are loaded from a config dict (editable via the Shop Rates
admin panel).  When no config is supplied, DEFAULT_CONFIG is used.
"""
import copy

# âââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ
#  DEFAULT CONFIGURATION  (seed values â overridden by admin panel)
# âââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ

DEFAULT_CONFIG = {
    # ââ Machine Rates ($/hr) ââââââââââââââââââââââââââââââââââââââ
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

    # ââ Setup Times (hr) ââââââââââââââââââââââââââââââââââââââââââ
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

    # ââ Laser Cut Speeds (IPM) ââââââââââââââââââââââââââââââââââââ
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

    # ââ Laser Capable Materials âââââââââââââââââââââââââââââââââââ
    # Materials the laser can cut, and max thickness (in)
    "laser_capable": {
        "carbon_steel":    1.000,
        "stainless_steel": 1.000,
        "aluminum":        0.500,
    },

    # ââ Water Jet Speeds (IPM) ââââââââââââââââââââââââââââââââââââ
    # key = "thickness_in", value = [standard, precision]
    "waterjet_speeds": {
        "0.250": [8.0, 4.0],
        "0.500": [4.0, 2.0],
        "1.000": [1.5, 0.75],
        "2.000": [0.6, 0.3],
    },

    # ââ Machinability Index (mild steel = 1.0) ââââââââââââââââââââ
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

    # ââ Press Brake âââââââââââââââââââââââââââââââââââââââââââââââ
    "bend_time_per_bend": {
        "brake_adira":  0.05,
        "brake_guifil": 0.06,
    },

    # ââ Welding (hr/weld-inch) ââââââââââââââââââââââââââââââââââââ
    "weld_rates": {
        "tig":   0.01058,
        "mig":   0.00280,
        "laser": 0.00444,
    },

    # ââ Production Ramp âââââââââââââââââââââââââââââââââââââââââââ
    # [qty, ramp_factor]  (lower = more time per piece)
    "ramp_table": [
        [1,    0.50],
        [10,   0.65],
        [25,   0.80],
        [50,   0.90],
        [100,  1.00],
        [1000, 1.00],
    ],

    # ââ Batch Handling ââââââââââââââââââââââââââââââââââââââââââââ
    "batch_threshold_hr": 6.0,
    "batch_handling": {
        "laser":        0.20,
        "water_jet":    0.20,
        "brake_adira":  0.15,
        "brake_guifil": 0.15,
        "tig_weld":     0.20,
    },

    # ââ Second Operator Rules âââââââââââââââââââââââââââââââââââââ
    "second_op_weight_lb": 50,
    "second_op_size_in":   60,

    # ââ Material Density (lb/in^3) ââââââââââââââââââââââââââââââââ
    "density": {
        "carbon_steel":    0.284,
        "stainless_steel": 0.289,
        "aluminum":        0.098,
        "copper":          0.323,
        "brass":           0.307,
        "titanium":        0.163,
    },

    # ââ Raw Material Cost ($/lb) ââââââââââââââââââââââââââââââââââ
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

    # ââ Hardware Insertion ââââââââââââââââââââââââââââââââââââââââ
    "hardware_time_per_insert": 0.015,
    "hardware_setup": 0.20,

    # ââ Tapping âââââââââââââââââââââââââââââââââââââââââââââââââââ
    "tap_time_per_hole": {
        "small":  0.008,
        "medium": 0.012,
        "large":  0.018,
    },
    "tap_setup": 0.20,

    # ââ Countersinking ââââââââââââââââââââââââââââââââââââââââââââ
    "csink_time_per_hole": 0.010,
    "csink_setup": 0.15,

    # ââ Passivation âââââââââââââââââââââââââââââââââââââââââââââââ
    "passivation_time_per_sqft": 0.04,
    "passivation_setup": 0.35,
    "passivation_min_charge": 0.25,
    "passivation_parts_per_batch": 50,

    # ââ Sawing ââââââââââââââââââââââââââââââââââââââââââââââââââââ
    "saw_time_per_cut": {
        "carbon_steel":    0.05,
        "stainless_steel": 0.08,
        "aluminum":        0.03,
        "copper":          0.04,
        "brass":           0.04,
        "titanium":        0.12,
    },

    # ââ CNC Material Removal Rates (in^3/min) ââââââââââââââââââââ
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

    # ââ Deburring âââââââââââââââââââââââââââââââââââââââââââââââââ
    "deburr_apex_hr_per_sqft": 0.08,
    "deburr_hand_hr_per_part": 0.20,
    "deburr_apex_max_thickness": 0.5,
    "deburr_apex_max_width": 32,

    # ââ Packaging / Final Inspection ââââââââââââââââââââââââââââââ
    "packaging_time_per_part": 0.005,
    "packaging_setup": 0.15,
    "packaging_rate": 125.00,

    # ââ Scrap Allowance âââââââââââââââââââââââââââââââââââââââââââ
    "scrap_allowance_pct": 15,

    # ââ Markups & Minimums ââââââââââââââââââââââââââââââââââââââââ
    "minimum_order_charge": 75.00,
    "rush_premium_pct": 50,
    "material_markup_pct": 15,
    "shop_markup_pct": 0,
}


def get_default_config():
    """Return a deep copy of the default config for seeding the database."""
    return copy.deepcopy(DEFAULT_CONFIG)


# âââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ
#  Helper functions
# âââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ

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


# âââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ
#  Main estimation function
# âââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ

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

    # ââ Sheet Metal Path ââââââââââââââââââââââââââââââââââââââââ
    if "sheet" in fab_type or "sheet metal" in fab_type:

        use_laser = _laser_can_cut(material, thickness, C)

        if use_laser:
            speed = _get_laser_speed(material, thickness, C)
            if speed and cut_perim > 0:
                cut_time_hr = (cut_perim / speed) / 60.0
            else:
                cut_time_hr = 0.35
            op_key = "laser"
            op_name = "Laser Cut (HSG G4020X 12kW)"
        else:
            speed = _get_waterjet_speed(material, thickness, config=C)
            if speed and cut_perim > 0:
                cut_time_hr = (cut_perim / speed) / 60.0
            else:
                cut_time_hr = 0.60
            op_key = "water_jet"
            op_name = "Water Jet Cut (OMAX 60120)"

        setup = setup_times.get(op_key, 0.55)
        rate = rates.get(op_key, 170.0)
        run_time = cut_time_hr / ramp * quantity
        batch_threshold = _cfg(C, "batch_threshold_hr")
        batch_handling = _cfg(C, "batch_handling")
        n_batch, batch_extra = _calc_batches(run_time, batch_handling.get(op_key, 0.15), batch_threshold)
        total_time = setup + run_time + batch_extra
        total_cost = total_time * rate

        operations.append({
            "operation": op_name,
            "op_key": op_key,
            "speed_ipm": round(speed, 1) if speed else None,
            "cycle_time_hr": round(cut_time_hr, 4),
            "setup_hr": round(setup, 2),
            "run_time_hr": round(run_time, 4),
            "batches": n_batch,
            "batch_handling_hr": round(batch_extra, 2),
            "total_time_hr": round(total_time, 4),
            "rate_per_hr": rate,
            "total_cost": round(total_cost, 2),
        })

        # 2. Bending
        if bend_count > 0:
            brake_key = "brake_adira"
            brake_name = "Press Brake Form (Adira 160T)"
            bend_times = _cfg(C, "bend_time_per_bend")
            time_per_bend = bend_times.get(brake_key, 0.05)
            bend_cycle = time_per_bend * bend_count
            bend_run = bend_cycle / ramp * quantity
            bend_setup = setup_times.get(brake_key, 0.55)
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
                "cycle_time_hr": round(bend_cycle, 4),
                "setup_hr": round(bend_setup, 2),
                "run_time_hr": round(bend_run, 4),
                "batches": n_b,
                "batch_handling_hr": round(b_extra, 2),
                "total_time_hr": round(bend_total_time, 4),
                "rate_per_hr": bend_rate,
                "second_operator": needs_2nd_op,
                "second_op_cost": round(second_op_cost, 2),
                "total_cost": round(bend_cost, 2),
            })

        # 3. Deburring
        flat_l = geometry.get("flat_length_in", 0) or 0
        flat_w = geometry.get("flat_width_in", 0) or 0
        if flat_l > 0 and flat_w > 0:
            area_sqft = (flat_l * flat_w) / 144.0
            apex_max_t = _cfg(C, "deburr_apex_max_thickness")
            apex_max_w = _cfg(C, "deburr_apex_max_width")
            if material == "stainless_steel" and thickness <= apex_max_t and flat_w <= apex_max_w:
                deburr_key = "deburr_apex"
                deburr_name = "Apex Deburr (304 SS)"
                deburr_cycle = _cfg(C, "deburr_apex_hr_per_sqft") * area_sqft
            else:
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

    # ââ Machined Part Path ââââââââââââââââââââââââââââââââââââââ
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
        l = dims.get("length", 0)
        w = dims.get("width", 0)
        h = dims.get("height", 0)
        sorted_dims = sorted([l, w, h], reverse=True)
        if sorted_dims[0] > 0 and sorted_dims[1] > 0:
            aspect = sorted_dims[0] / max(sorted_dims[1], 0.01)
        else:
            aspect = 1.0

        if aspect > 2.0 and max(sorted_dims[1], sorted_dims[2]) <= 12.5:
            op_key = "st30_turning"
            op_name = "CNC Turning (Haas ST-30)"
            mrr_table = _cfg(C, "mrr_turning")
            mrr = mrr_table.get(material, 1.8)
        else:
            op_key = "tm2p_milling"
            op_name = "CNC Milling (Haas TM-2P)"
            mrr_table = _cfg(C, "mrr_milling")
            mrr = mrr_table.get(material, 0.8)

        if volume_removed > 0 and mrr > 0:
            cycle = (volume_removed / mrr) / 60.0
        else:
            cycle = 0.25

        setup = setup_times.get(op_key, 0.65)
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

    # ââ Welding âââââââââââââââââââââââââââââââââââââââââââââââââ
    if weld_length > 0:
        weld_method = "tig"
        weld_key = "tig_weld"
        weld_name = "TIG Weld (default)"
        weld_rates_cfg = _cfg(C, "weld_rates")
        weld_cycle = weld_rates_cfg.get(weld_method, 0.01058) * weld_length
        weld_run = weld_cycle / ramp * quantity
        weld_setup = setup_times.get(weld_key, 0.30)
        weld_total = weld_setup + weld_run
        weld_rate = rates.get(weld_key, 131.0)
        weld_cost = weld_total * weld_rate

        operations.append({
            "operation": weld_name,
            "op_key": weld_key,
            "weld_length_in": weld_length,
            "cycle_time_hr": round(weld_cycle, 4),
            "setup_hr": round(weld_setup, 2),
            "run_time_hr": round(weld_run, 4),
            "total_time_hr": round(weld_total, 4),
            "rate_per_hr": weld_rate,
            "total_cost": round(weld_cost, 2),
        })

    # ââ Material Cost ââââââââââââââââââââââââââââââââââââââââââ
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

    # ââ Packaging / Final Inspection âââââââââââââââââââââââââââ
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

    # ââ Totals ââââââââââââââââââââââââââââââââââââââââââââââââââ
    total_cost = sum(op["total_cost"] for op in operations)
    total_time = sum(op["total_time_hr"] for op in operations)
    unit_cost = total_cost / max(quantity, 1)

    # ââ Quantity Breaks âââââââââââââââââââââââââââââââââââââââââ
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
    """Simplified cost calc for quantity break table (no recursion)."""
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
        else:
            speed = _get_waterjet_speed(material, thickness, config=C)
            cut_time = (cut_perim / speed / 60.0) if speed and cut_perim > 0 else 0.60
            op_key = "water_jet"

        run = cut_time / ramp * quantity
        _, b_extra = _calc_batches(run, batch_handling.get(op_key, 0.15), batch_threshold)
        t = setup_times.get(op_key, 0.55) + run + b_extra
        total_time += t
        total_cost += t * rates.get(op_key, 170.0)

        if bend_count > 0:
            bk = "brake_adira"
            bend_times = _cfg(C, "bend_time_per_bend")
            bc = bend_times.get(bk, 0.05) * bend_count
            br = bc / ramp * quantity
            _, be = _calc_batches(br, 0.15, batch_threshold)
            bt = setup_times.get(bk, 0.55) + br + be
            total_time += bt
            total_cost += bt * rates.get(bk, 157.0)
            second_op_weight = _cfg(C, "second_op_weight_lb")
            second_op_size = _cfg(C, "second_op_size_in")
            if weight > second_op_weight or max_dim > second_op_size:
                total_cost += br * rates.get("labor_only", 125.0)

        flat_l = geometry.get("flat_length_in", 0) or 0
        flat_w = geometry.get("flat_width_in", 0) or 0
        if flat_l > 0 and flat_w > 0:
            apex_max_t = _cfg(C, "deburr_apex_max_thickness")
            apex_max_w = _cfg(C, "deburr_apex_max_width")
            if material == "stainless_steel" and thickness <= apex_max_t and flat_w <= apex_max_w:
                dc = _cfg(C, "deburr_apex_hr_per_sqft") * (flat_l * flat_w) / 144.0
            else:
                dc = _cfg(C, "deburr_hand_hr_per_part")
            dr = dc / ramp * quantity
            dt = setup_times.get("deburr_apex", 0.25) + dr
            total_time += dt
            total_cost += dt * rates.get("deburr_apex", 125.76)

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
        l = dims.get("length", 0)
        w = dims.get("width", 0)
        h = dims.get("height", 0)
        sorted_dims = sorted([l, w, h], reverse=True)
        if sorted_dims[0] > 0 and sorted_dims[1] > 0:
            aspect = sorted_dims[0] / max(sorted_dims[1], 0.01)
        else:
            aspect = 1.0

        if aspect > 2.0 and max(sorted_dims[1], sorted_dims[2]) <= 12.5:
            op_key = "st30_turning"
            mrr = _cfg(C, "mrr_turning").get(material, 1.8)
        else:
            op_key = "tm2p_milling"
            mrr = _cfg(C, "mrr_milling").get(material, 0.8)

        cycle = max((volume / mrr / 60.0), 0.25) if volume > 0 and mrr > 0 else 0.25
        run = cycle / ramp * quantity
        t = setup_times.get(op_key, 0.65) + run
        total_time += t
        total_cost += t * rates.get(op_key, 150.0)

    if weld_length > 0:
        weld_rates_cfg = _cfg(C, "weld_rates")
        wc = weld_rates_cfg.get("tig", 0.01058) * weld_length
        wr = wc / ramp * quantity
        wt = setup_times.get("tig_weld", 0.30) + wr
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
