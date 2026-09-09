"""
CMC Shop Cost Engine v1.0
Estimates fabrication cost from extracted geometry using the Shop Standard Time Database.

Inputs: geometry dict (from STEP or PDF extraction) + material + quantity
Outputs: cost breakdown with routing, time, and price per operation
"""

# ââ All-In Rates ($/hr) âââââââââââââââââââââââââââââââââââââââââââââ
# From Master Operation DB: labor + burden + depreciation + electricity
RATES = {
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
}

# ââ Setup Times (hr) ââââââââââââââââââââââââââââââââââââââââââââââââ
# From Master Operation DB: programming + material pull + staging + accounting + QA
SETUP = {
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
}

# ââ Laser Cutting Speeds (in/min, standard edge, 20% reduced from published) ââ
# HSG G4020X 12kW fiber laser
LASER_SPEEDS = {
    # (material, thickness_in): speed_ipm
    ("carbon_steel", 0.125): 320,
    ("carbon_steel", 0.250): 160,
    ("carbon_steel", 0.500):  80,
    ("carbon_steel", 0.750):  48,
    ("carbon_steel", 1.000):  28,
    ("stainless_steel", 0.125): 240,
    ("stainless_steel", 0.250): 112,
    ("stainless_steel", 0.500):  48,
    ("stainless_steel", 0.750):  24,
    ("stainless_steel", 1.000):  14.4,
}

# ââ Water Jet Cutting Speeds (in/min, mild steel baseline) ââââââââââ
# OMAX 60120 w/ A-Jet
WATERJET_SPEEDS_STEEL = {
    # thickness_in: (standard_tol, precision_tol)
    0.250: (8.0, 4.0),
    0.500: (4.0, 2.0),
    1.000: (1.5, 0.75),
    2.000: (0.6, 0.3),
}

# Machinability index (mild steel = 1.0)
MACHINABILITY = {
    "carbon_steel":    1.0,
    "stainless_steel": 0.9,
    "aluminum":        2.9,
    "wood":            5.0,
    "uhmw":            6.0,
    "acetal":          5.5,
}

# ââ Press Brake âââââââââââââââââââââââââââââââââââââââââââââââââââââ
BEND_TIME_PER_BEND = {
    "brake_adira":  0.05,   # hr/bend
    "brake_guifil": 0.06,   # hr/bend
}

# ââ Welding âââââââââââââââââââââââââââââââââââââââââââââââââââââââââ
WELD_RATES = {
    # method: effective hr/weld-inch
    "tig":   0.01058,
    "mig":   0.00280,
    "laser": 0.00444,
}

# ââ Production Ramp âââââââââââââââââââââââââââââââââââââââââââââââââ
# qty -> production rate (% of full speed). Lower = more time per piece.
RAMP_TABLE = [
    (1,    0.50),
    (10,   0.65),
    (25,   0.80),
    (50,   0.90),
    (100,  1.00),
    (1000, 1.00),
]

# ââ Batch Threshold âââââââââââââââââââââââââââââââââââââââââââââââââ
BATCH_THRESHOLD_HR = 6.0
BATCH_HANDLING = {
    "laser":        0.20,
    "water_jet":    0.20,
    "brake_adira":  0.15,
    "brake_guifil": 0.15,
    "tig_weld":     0.20,
}

# ââ Second Operator Rules âââââââââââââââââââââââââââââââââââââââââââ
SECOND_OP_WEIGHT_LB = 50
SECOND_OP_SIZE_IN   = 60

# ââ Material density (lb/in^3) for weight estimation ââââââââââââââââ
DENSITY = {
    "carbon_steel":    0.284,
    "stainless_steel": 0.289,
    "aluminum":        0.098,
    "copper":          0.323,
    "brass":           0.307,
    "titanium":        0.163,
}


# âââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ
#  Helper functions
# âââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ

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
    return "carbon_steel"  # default


def _get_ramp_factor(qty):
    """Interpolate production ramp factor for a given quantity."""
    if qty <= 1:
        return 0.50
    prev_q, prev_r = 1, 0.50
    for q, r in RAMP_TABLE:
        if qty <= q:
            # linear interpolation
            frac = (qty - prev_q) / max(q - prev_q, 1)
            return prev_r + frac * (r - prev_r)
        prev_q, prev_r = q, r
    return 1.0


def _find_nearest(table_keys, value):
    """Find the nearest key in a list of numeric keys."""
    return min(table_keys, key=lambda k: abs(k - value))


def _calc_batches(run_time_hr, handling_hr_per_batch):
    """Calculate number of batches and extra handling time."""
    if run_time_hr <= BATCH_THRESHOLD_HR:
        return 1, 0.0
    n_batches = max(1, int(run_time_hr / BATCH_THRESHOLD_HR))
    extra = (n_batches - 1) * handling_hr_per_batch
    return n_batches, extra


def _laser_can_cut(material, thickness_in):
    """Check if laser can cut this material/thickness."""
    if material in ("carbon_steel", "stainless_steel"):
        return thickness_in <= 1.0
    return False  # plastics, wood, etc. -> water jet


def _get_laser_speed(material, thickness_in):
    """Get laser cutting speed in in/min for material and thickness."""
    thicknesses = [t for (m, t) in LASER_SPEEDS if m == material]
    if not thicknesses:
        return None
    nearest = _find_nearest(thicknesses, thickness_in)
    speed = LASER_SPEEDS.get((material, nearest))
    # Scale for thickness difference if not exact match
    if speed and nearest != thickness_in and nearest > 0:
        ratio = nearest / thickness_in
        # Thicker = slower, roughly linear for small differences
        if ratio > 1:
            speed = speed * min(ratio, 1.5)
        else:
            speed = speed * max(ratio, 0.5)
    return speed


def _get_waterjet_speed(material, thickness_in, precision=False):
    """Get water jet cutting speed in in/min."""
    thicknesses = list(WATERJET_SPEEDS_STEEL.keys())
    nearest = _find_nearest(thicknesses, thickness_in)
    std, prec = WATERJET_SPEEDS_STEEL[nearest]
    base = prec if precision else std
    # Apply machinability index
    mac = MACHINABILITY.get(material, 1.0)
    return base * mac


# âââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ
#  Main estimation function
# âââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ

def estimate_cost(geometry, material_str, quantity=1):
    """
    Estimate fabrication cost from extracted geometry.

    Args:
        geometry: dict with keys from STEP or PDF extraction:
            - fab_type: "Sheet Metal" or "Machined"
            - thickness_in: float (inches)
            - dims: dict with length, width, height (inches)
            - bend_count: int
            - cut_perimeter_in: float (total cut perimeter in inches)
            - flat_length_in, flat_width_in: float (developed flat dims)
            - weight_lb: float (estimated weight)
            - hole_count: int
            - volume_in3: float
            - weld_length_in: float (optional)
        material_str: user-selected material string
        quantity: int, number of parts

    Returns:
        dict with cost breakdown
    """
    material = _normalize_material(material_str)
    fab_type = (geometry.get("fab_type") or "Sheet Metal").lower()
    thickness = geometry.get("thickness_in", 0.0) or 0.0
    dims = geometry.get("dims", {})
    max_dim = max(dims.get("length", 0), dims.get("width", 0), dims.get("height", 0))
    weight = geometry.get("weight_lb", 0.0) or 0.0
    bend_count = geometry.get("bend_count", 0) or 0
    cut_perim = geometry.get("cut_perimeter_in", 0.0) or 0.0
    hole_count = geometry.get("hole_count", 0) or 0
    weld_length = geometry.get("weld_length_in", 0.0) or 0.0

    ramp = _get_ramp_factor(quantity)
    operations = []
    warnings = []

    # ââ Sheet Metal Path ââââââââââââââââââââââââââââââââââââââââ
    if "sheet" in fab_type or "sheet metal" in fab_type:

        # 1. Cutting operation (laser vs water jet)
        use_laser = _laser_can_cut(material, thickness)

        if use_laser:
            speed = _get_laser_speed(material, thickness)
            if speed and cut_perim > 0:
                cut_time_hr = (cut_perim / speed) / 60.0
            else:
                cut_time_hr = 0.35  # fallback placeholder
            op_key = "laser"
            op_name = "Laser Cut (HSG G4020X 12kW)"
        else:
            speed = _get_waterjet_speed(material, thickness)
            if speed and cut_perim > 0:
                cut_time_hr = (cut_perim / speed) / 60.0
            else:
                cut_time_hr = 0.60  # fallback placeholder
            op_key = "water_jet"
            op_name = "Water Jet Cut (OMAX 60120)"

        setup = SETUP[op_key]
        rate = RATES[op_key]
        run_time = cut_time_hr / ramp * quantity
        n_batch, batch_extra = _calc_batches(run_time, BATCH_HANDLING.get(op_key, 0.15))
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

        # 2. Bending (if bends detected)
        if bend_count > 0:
            brake_key = "brake_adira"  # default to Adira (160 ton, larger)
            brake_name = "Press Brake Form (Adira 160T)"
            time_per_bend = BEND_TIME_PER_BEND[brake_key]
            bend_cycle = time_per_bend * bend_count
            bend_run = bend_cycle / ramp * quantity
            bend_setup = SETUP[brake_key]
            n_b, b_extra = _calc_batches(bend_run, BATCH_HANDLING.get(brake_key, 0.15))
            bend_total_time = bend_setup + bend_run + b_extra
            bend_rate = RATES[brake_key]
            bend_cost = bend_total_time * bend_rate

            # Second operator check
            needs_2nd_op = weight > SECOND_OP_WEIGHT_LB or max_dim > SECOND_OP_SIZE_IN
            if needs_2nd_op:
                second_op_cost = bend_run * RATES["labor_only"]
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

        # 3. Deburring (estimate based on part area)
        flat_l = geometry.get("flat_length_in", 0) or 0
        flat_w = geometry.get("flat_width_in", 0) or 0
        if flat_l > 0 and flat_w > 0:
            area_sqft = (flat_l * flat_w) / 144.0
            # Route: Apex for stainless <= 1/2" and <= 32" wide
            if material == "stainless_steel" and thickness <= 0.5 and flat_w <= 32:
                deburr_key = "deburr_apex"
                deburr_name = "Apex Deburr (304 SS)"
                deburr_cycle = 0.08 * area_sqft  # hr/sqft
            else:
                deburr_key = "deburr_hand"
                deburr_name = "Hand Deburr"
                deburr_cycle = 0.20  # hr/part flat rate

            deburr_run = deburr_cycle / ramp * quantity
            deburr_setup = SETUP.get(deburr_key, 0.25)
            deburr_total = deburr_setup + deburr_run
            deburr_rate = RATES[deburr_key]
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

    # ââ Machined Part Path ââââââââââââââââââââââââââââââââââââââ
    elif "machin" in fab_type:
        volume_removed = geometry.get("volume_in3", 0.0) or 0.0

        # Turning (if roughly cylindrical - height >> width ~= length)
        l = dims.get("length", 0)
        w = dims.get("width", 0)
        h = dims.get("height", 0)
        sorted_dims = sorted([l, w, h], reverse=True)

        if sorted_dims[0] > 0 and sorted_dims[1] > 0:
            aspect = sorted_dims[0] / max(sorted_dims[1], 0.01)
        else:
            aspect = 1.0

        # Simple heuristic: if longest dim is > 2x the other two, likely turned
        if aspect > 2.0 and max(sorted_dims[1], sorted_dims[2]) <= 12.5:
            # Turning path (ST-30)
            op_key = "st30_turning"
            op_name = "CNC Turning (Haas ST-30)"
            # MRR estimate: 304 SS at 250 SFM, 0.008 ipr, 0.075 DOC
            mrr = 1.8  # in3/min rough average
            if volume_removed > 0:
                cycle = (volume_removed / mrr) / 60.0
            else:
                cycle = 0.25  # placeholder
        else:
            # Milling path (TM-2P)
            op_key = "tm2p_milling"
            op_name = "CNC Milling (Haas TM-2P)"
            mrr = 0.8  # in3/min rough average for 304 SS milling
            if volume_removed > 0:
                cycle = (volume_removed / mrr) / 60.0
            else:
                cycle = 0.30  # placeholder

        setup = SETUP[op_key]
        rate = RATES[op_key]
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

    # ââ Welding (if weld length provided) âââââââââââââââââââââââ
    if weld_length > 0:
        weld_method = "tig"
        weld_key = "tig_weld"
        weld_name = "TIG Weld (default)"
        weld_cycle = WELD_RATES[weld_method] * weld_length
        weld_run = weld_cycle / ramp * quantity
        weld_setup = SETUP[weld_key]
        weld_total = weld_setup + weld_run
        weld_rate = RATES[weld_key]
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
            qb = estimate_cost_simple(geometry, material_str, qty)
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


def estimate_cost_simple(geometry, material_str, quantity=1):
    """
    Wrapper that computes cost without recursive qty_breaks.
    Used internally to avoid infinite recursion in qty break calculation.
    """
    material = _normalize_material(material_str)
    fab_type = (geometry.get("fab_type") or "Sheet Metal").lower()
    thickness = geometry.get("thickness_in", 0.0) or 0.0
    dims = geometry.get("dims", {})
    max_dim = max(dims.get("length", 0), dims.get("width", 0), dims.get("height", 0))
    weight = geometry.get("weight_lb", 0.0) or 0.0
    bend_count = geometry.get("bend_count", 0) or 0
    cut_perim = geometry.get("cut_perimeter_in", 0.0) or 0.0
    weld_length = geometry.get("weld_length_in", 0.0) or 0.0

    ramp = _get_ramp_factor(quantity)
    total_cost = 0.0
    total_time = 0.0

    if "sheet" in fab_type or "sheet metal" in fab_type:
        use_laser = _laser_can_cut(material, thickness)
        if use_laser:
            speed = _get_laser_speed(material, thickness)
            cut_time = (cut_perim / speed / 60.0) if speed and cut_perim > 0 else 0.35
            op_key = "laser"
        else:
            speed = _get_waterjet_speed(material, thickness)
            cut_time = (cut_perim / speed / 60.0) if speed and cut_perim > 0 else 0.60
            op_key = "water_jet"

        run = cut_time / ramp * quantity
        _, batch_extra = _calc_batches(run, BATCH_HANDLING.get(op_key, 0.15))
        t = SETUP[op_key] + run + batch_extra
        total_time += t
        total_cost += t * RATES[op_key]

        if bend_count > 0:
            bk = "brake_adira"
            bc = BEND_TIME_PER_BEND[bk] * bend_count
            br = bc / ramp * quantity
            _, be = _calc_batches(br, 0.15)
            bt = SETUP[bk] + br + be
            total_time += bt
            total_cost += bt * RATES[bk]
            if weight > SECOND_OP_WEIGHT_LB or max_dim > SECOND_OP_SIZE_IN:
                total_cost += br * RATES["labor_only"]

    elif "machin" in fab_type:
        volume = geometry.get("volume_in3", 0.0) or 0.0
        cycle = max((volume / 1.8 / 60.0), 0.25) if volume > 0 else 0.25
        op_key = "st30_turning"
        run = cycle / ramp * quantity
        t = SETUP[op_key] + run
        total_time += t
        total_cost += t * RATES[op_key]

    if weld_length > 0:
        wc = WELD_RATES["tig"] * weld_length
        wr = wc / ramp * quantity
        wt = SETUP["tig_weld"] + wr
        total_time += wt
        total_cost += wt * RATES["tig_weld"]

    unit_cost = total_cost / max(quantity, 1)
    return {"total_cost": round(total_cost, 2), "unit_cost": round(unit_cost, 2), "total_time_hr": round(total_time, 4)}
