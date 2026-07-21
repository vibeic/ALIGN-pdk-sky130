import json
import logging
from math import sqrt, floor, ceil, log10
from copy import deepcopy
logger = logging.getLogger(__name__)

# sky130 layout grid, in nm.
SKY130_GRID = 5
# sky130 minimum TRANSISTOR width (diffusion under a gate), in nm. This is NOT
# difftap.1 (0.15um), which is the minimum width of a bare diff/tap shape; a
# diff strip crossed by poly is a FET and carries the stricter rule. Taken from
# the PDK's own magic tech file, sky130A.tech:
#
#     edge4way *poly allfetsstd 420 allfets 0 0 \
#         "Transistor width < %d (diff/tap.2)"
#
# (0.36um is allowed inside standard cells and 0.21/0.14um inside SRAM cores;
# neither waiver applies to an analog primitive, so the 420nm bound is the one
# that governs here.) Measured: an array built at 330nm draws 600 magic
# "Transistor width < 420 (diff/tap.2)" violations. Note that sky130A_mr.drc,
# the klayout deck, contains no FET-width rule at all and reports 0 on the same
# layout -- magic is the only one of the two that catches this.
SKY130_FET_MIN_WIDTH = 420


def _pdk_layer(pdk_data, name):
    """Return the Abstraction entry for a layer, or None."""
    for layer in pdk_data.get("Abstraction", []):
        if layer.get("Layer") == name:
            return layer
    return None


def _drawn_width_bounds(pdk_data, design_config):
    """Return (min_nm, max_nm, quantum_nm) for a DRAWN transistor width on a
    planar bulk PDK. Every bound is derived from this PDK's own data.

    min
        The diffusion strip has to satisfy the process minimum FET width
        (diff/tap.2, 420nm) AND be wide enough to enclose one source/drain
        contact: ``mos.py`` places V0 cuts inside the strip with
        ``V0['VencA_L']`` enclosure on each side, so anything narrower than
        ``V0['WidthY'] + 2*V0['VencA_L']`` pushes the cut outside the diffusion.
        On this PDK the process rule (420nm) is the binding one; the contact
        bound (330nm) is kept so the check stays correct if either changes.

        Note that ``mos.py`` already carried this bound, by accident and in the
        wrong units: ``assert fin > 1`` rejects a one-row device, and one row is
        210nm, so two rows is 420nm. Stating it as a width makes it checkable
        against the actual netlist value and gives a message that names the
        device.

    max
        ``design_info['unit_size_mos']`` rows of the generator's vertical grid.
        This is the same bound ``mos.py`` already enforces as
        ``finDummy = (height - fin)//2 >= 8`` with the default height of 28;
        stating it here just turns a cryptic downstream assert into a message
        that names the device. The FinFET14nm PDK shipped with ALIGN uses
        ``unit_size_mos`` for exactly this purpose.

    quantum
        ``mos.py`` centres the diffusion strip on a fixed offset
        (``unitCellHeight//2 - Fin_pitch//2``), so both of its edges land on the
        5nm grid only if half the width is a whole number of grid steps. ALIGN's
        own ``CenterLineGrid.addCenterLine`` independently asserts
        ``width % 2 == 0``. Both are satisfied by a 10nm quantum -- 21x finer
        than the 210nm the fin-count code path used to demand.
    """
    v0 = _pdk_layer(pdk_data, "V0")
    contact_min = v0["WidthY"] + 2 * v0["VencA_L"] if v0 else 0
    min_nm = max(SKY130_FET_MIN_WIDTH, contact_min)
    max_nm = design_config["unit_size_mos"] * design_config["Fin_pitch"]
    return min_nm, max_nm, 2 * SKY130_GRID


def limit_pairs(pairs):
    # Hack to limit aspect ratios when there are a lot of choices
    if len(pairs) > 12:
        new_pairs = []
        log10_aspect_ratios = [-0.3, 0, 0.3]
        for l in log10_aspect_ratios:
            best_pair = min((abs(log10(newy) - log10(newx) - l), (newx, newy))
                            for newx, newy in pairs)[1]
            new_pairs.append(best_pair)
        return new_pairs
    else:
        return pairs

def add_primitive(primitives, block_name, block_args):
    if block_name in primitives:
        if not primitives[block_name] == block_args:
            logger.warning(f"Distinct devices mapped to the same primitive {block_name}: \
                             existing: {primitives[block_name]}\
                             new: {block_args}")
    else:
        logger.debug(f"Found primitive {block_name} with {block_args}")
        if 'x_cells' in block_args and 'y_cells' in block_args:
                x, y = block_args['x_cells'], block_args['y_cells']
                pairs = set()
                m = x*y
                y_sqrt = floor(sqrt(x*y))
                for y in range(y_sqrt, 0, -1):
                    if m % y == 0:
                        pairs.add((y, m//y))
                        pairs.add((m//y, y))
                    if y == 1:
                        break
                pairs = limit_pairs((pairs))
                mv = block_args.get('parameters') or {}
                if len(mv) == 2:
                    fpair = [int(v.get("NF", 1)) * int(v.get("M", 1)) for v in mv.values()]
                    if fpair[0] != fpair[1]:
                        # ratioed pair: keep only shapes whose column count
                        # represents the ratio exactly (pattern is per-column)
                        tot, fs = sum(fpair), min(fpair)
                        ok = [(nx, ny) for nx, ny in pairs
                              if nx * fs % tot == 0 and nx * fs >= tot]
                        pairs = ok or [(block_args['x_cells'], block_args['y_cells'])]
                for newx, newy in pairs:
                    concrete_name = f'{block_name}_X{newx}_Y{newy}'
                    if concrete_name not in primitives:
                        primitives[concrete_name] = deepcopy(block_args)
                        primitives[concrete_name]['x_cells'] = newx
                        primitives[concrete_name]['y_cells'] = newy
                        primitives[concrete_name]['abstract_template_name'] = block_name
                        primitives[concrete_name]['concrete_template_name'] = concrete_name
        else:
            primitives[block_name] = block_args
            primitives[block_name]['abstract_template_name'] = block_name
            primitives[block_name]['concrete_template_name'] = block_name

def gen_param(subckt, primitives, pdk_dir):
    block_name = subckt.name
    vt = subckt.elements[0].model
    values = subckt.elements[0].parameters
    generator_name = subckt.generator["name"]
    block_name = subckt.name
    generator_name = subckt.generator["name"]
    layers_json = pdk_dir / "layers.json"
    with open(layers_json, "rt") as fp:
        pdk_data = json.load(fp)
    design_config = pdk_data["design_info"]

    if len(subckt.elements) == 1:
        values = subckt.elements[0].parameters
    else:
        mvalues = {}
        for ele in subckt.elements:
            mvalues[ele.name] = ele.parameters

    if generator_name == 'CAP':

        size = round(float(values["VALUE"]) * 1E15, 4)

        assert size <= design_config["max_size_cap"], f"caps larger than {design_config['max_size_cap']}fF are not supported"

        if "L" in values and "W" in values:
            length = round(float(values["L"]) * 1E9, 4)
            width = round(float(values["W"]) * 1E9, 4)
        else:
            # HACK for unit cap used in common centroid and support older SPICE
            length = int((sqrt(size/2))*1000)
            if length % 2 > 0 : length += 1
            width = int((sqrt(size/2))*1000)
            if width % 2 > 0 : width += 1

        # TODO: use float in name
        logger.debug(f"Generating capacitor for:{block_name}, {size}")
        block_args = {
            'primitive': generator_name,
            'value':  [int(length), int(width)]
        }
        add_primitive(primitives, block_name, block_args)

    elif generator_name == 'RES':
        assert float(values["VALUE"]) or float(values["R"]), f"unidentified size {values['VALUE']} for {name}"
        if "R" in values:
            size = round(float(values["R"]), 2)
        elif 'VALUE' in values:
            size = round(float(values["VALUE"]), 2)
        # TODO: use float in name
        if size.is_integer():
            size = int(size)
        height = ceil(sqrt(float(size) / design_config["unit_height_res"]))
        logger.debug(f'Generating resistor for: {block_name} {size}')
        block_args = {
            'primitive': generator_name,
            'value': (height, float(size))
        }
        add_primitive(primitives, block_name, block_args)

    else:
        # DoNotIdentify'd lone transistors arrive with their model name as
        # the generator (NMOS_RVT/PMOS_LVT/...); they are plain MOS arrays.
        assert 'MOS' in generator_name, f'{generator_name} is not recognized'
        generator_name = 'MOS'
        if "vt_type" in design_config:
            vt = [vt.upper() for vt in design_config["vt_type"] if vt.upper() in subckt.elements[0].model]
        mvalues = {}
        for ele in subckt.elements:
            mvalues[ele.name] = ele.parameters
        device_name_all = [*mvalues.keys()]
        device_name = next(iter(mvalues))

        # sky130 is a planar BULK process -- design_info says pdk_type "Bulk"
        # and mos.py never draws a single shape on the Fin layer; it only uses
        # the Fin centre-line grid as a 210nm vertical ruler for the select /
        # nwell regions. So W is a DRAWN diffusion width here, not a fin count,
        # and quantising it to the fin pitch imposes a FinFET constraint on a
        # technology that has none. The bounds below are the real ones.
        #
        # The FinFET branch is kept because this file is the template every
        # ALIGN PDK copies; on a genuine fin process W really is a fin count.
        pdk_type = design_config.get("pdk_type", "FinFET")
        fin_pitch = design_config["Fin_pitch"]
        if pdk_type == "Bulk":
            min_w, max_w, quantum = _drawn_width_bounds(pdk_data, design_config)
        for key in mvalues:
            assert mvalues[key]["W"] != str, f"unrecognized size of device {key}:{mvalues[key]['W']} in {block_name}"
            width_nm = float(mvalues[key]["W"]) * 1E+9
            if pdk_type == "Bulk":
                assert abs(width_nm - round(width_nm)) < 1e-6, \
                    f"Width of device {key} in {block_name} ({mvalues[key]['W']}) does not resolve to an integer nm value"
                width_nm = int(round(width_nm))
                assert width_nm >= min_w, \
                    f"Width of device {key} in {block_name} is {width_nm}nm; " \
                    f"the minimum drawn width on this PDK is {min_w}nm"
                assert width_nm <= max_w, \
                    f"Width of device {key} in {block_name} is {width_nm}nm; " \
                    f"the maximum drawn width that fits a unit cell on this PDK is {max_w}nm " \
                    f"(use M/NF to build wider devices)"
                assert width_nm % quantum == 0, \
                    f"Width of device {key} in {block_name} is {width_nm}nm; " \
                    f"drawn widths must be a multiple of {quantum}nm on this PDK"
                # Rows of the generator's vertical grid this device occupies.
                # It is an allocation, NOT the drawn width: mos.py reads W from
                # the netlist parameters and draws the diffusion at exactly that
                # width. For a W that IS a multiple of the fin pitch this is the
                # same number the old fin-count code produced, so every device
                # that built before still builds identically.
                size = ceil(width_nm / fin_pitch)
            else:
                assert int(width_nm) % fin_pitch == 0, \
                    f"Width of device {key} in {block_name} should be multiple of fin pitch:{fin_pitch}"
                size = int(width_nm / fin_pitch)
            mvalues[key]["NFIN"] = size
        name_arg = 'NFIN'+str(size)

        if 'NF' in mvalues[device_name].keys():
            for key in mvalues:
                assert int(mvalues[key]["NF"]), f"unrecognized NF of device {key}:{mvalues[key]['NF']} in {subckt.name}"
                assert int(mvalues[key]["NF"]) % 2 == 0, f"NF must be even for device {key}:{mvalues[key]['NF']} in {subckt.name}"
            name_arg = name_arg+'_NF'+str(int(mvalues[device_name]["NF"]))

        if 'M' in mvalues[device_name].keys():
            for key in mvalues:
                assert int(mvalues[key]["M"]), f"unrecognized M of device {key}:{mvalues[key]['M']} in {subckt.name}"
                if "PARALLEL" in mvalues[key].keys() and int(mvalues[key]['PARALLEL']) > 1:
                    mvalues[key]["PARALLEL"] = int(mvalues[key]['PARALLEL'])
                    mvalues[key]['M'] = int(mvalues[key]['M'])*int(mvalues[key]['PARALLEL'])
            name_arg = name_arg+'_M'+str(int(mvalues[device_name]["M"]))
            size = 0

        logger.debug(f"Generating lef for {block_name}")
        if isinstance(size, int):
            for key in mvalues:
                # Compare W itself, not the NFIN allocation. On a bulk PDK NFIN
                # is ceil(W/fin_pitch), so two DIFFERENT widths can share one
                # allocation (1000nm and 1050nm both allocate 5 rows) -- and
                # mos.py draws one width for the whole primitive, so they must
                # genuinely be equal.
                assert float(mvalues[device_name]["W"]) == float(mvalues[key]["W"]), f"W should be same for all devices in {subckt.name} {mvalues}"
                size_device = int(mvalues[key]["NF"])*int(mvalues[key]["M"])
                size = size + size_device
            no_units = ceil(size / (2*len(mvalues)))  # Factor 2 is due to NF=2 in each unit cell; needs to be generalized
            if any(x in block_name for x in ['DP', '_S']) and floor(sqrt(no_units/3)) >= 1:
                square_y = floor(sqrt(no_units/3))
            else:
                square_y = floor(sqrt(no_units))
            while no_units % square_y != 0:
                square_y -= 1
            yval = square_y
            xval = int(no_units / square_y)

        if len(mvalues) == 2:
            f0 = int(mvalues[device_name_all[0]]["NF"])*int(mvalues[device_name_all[0]]["M"])
            f1 = int(mvalues[device_name_all[1]]["NF"])*int(mvalues[device_name_all[1]]["M"])
            if f0 != f1:
                # ratioed pair (SCM/CMC mirror): x_cells must carry the TRUE
                # total unit count. The generic no_units above splits units
                # equally between the pair, drawing ratioed mirrors ~1:1.
                # generate_MOS_primitive builds an exact per-column pattern
                # from each device's NF*M and skips its 2x doubling.
                yval = 1
                xval = ceil(size / 2)  # size = total fingers, 2 per unit

        block_args = {
            'primitive': generator_name,
            'value': mvalues[device_name]["NFIN"],
            'x_cells': xval,
            'y_cells': yval,
            'parameters': mvalues
        }
        if 'STACK' in mvalues[device_name].keys() and int(mvalues[device_name]["STACK"]) > 1:
            block_args['stack'] = int(mvalues[device_name]["STACK"])
        if vt:
            block_args['vt_type'] = vt[0]
        add_primitive(primitives, block_name, block_args)
    return True
