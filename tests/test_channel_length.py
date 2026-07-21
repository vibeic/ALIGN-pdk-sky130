"""Regression guard: the MOS generator must honour the netlist channel length L.

Historically the sky130 MOS generator ignored L entirely and drew every gate at
the fixed pdk['Poly']['Width'] = 150nm, so two netlists differing ONLY in L
produced byte-identical geometry. These tests fail if that ever comes back.

NEGATIVE CONTROL
----------------
`test_negative_control_detects_ignored_L` proves the assertion used by
`test_drawn_gate_length_matches_netlist` is actually capable of failing: it
feeds the checker a layout generated at the nominal length while claiming a
long L, which is exactly the signature of the original bug, and requires the
checker to reject it. A guard that cannot fail is worthless.

Run:  pytest -v tests/test_channel_length.py
"""
import json
import os
import pathlib
import shutil
import subprocess
import sys

import pytest

PDK_DIR = pathlib.Path(__file__).resolve().parent.parent / "SKY130_PDK"
EXAMPLES = pathlib.Path(__file__).resolve().parent.parent / "examples"
POLY_LAYER, POLY_DATATYPE = 66, 20

# L values in nm to exercise. 150 is nominal (must be a strict no-op);
# 500 and 1000 land on different poly-pitch multiples (k=2 and k=3).
L_VALUES_NM = [150, 500, 1000]


def _drawn_gate_lengths(gds_json_path):
    """Return {x_extent_nm: count} for VERTICAL poly shapes in a cell.

    The horizontal Pc gate-contact straps share GDS layer 66, so vertical gates
    are separated from horizontal straps by aspect ratio.
    """
    with open(gds_json_path) as fp:
        data = json.load(fp)
    hist = {}
    for el in data["bgnlib"][0]["bgnstr"][0]["elements"]:
        if el.get("type") != "boundary":
            continue
        if el.get("layer") != POLY_LAYER or el.get("datatype") != POLY_DATATYPE:
            continue
        xs, ys = el["xy"][0::2], el["xy"][1::2]
        w, h = max(xs) - min(xs), max(ys) - min(ys)
        if h >= w:                      # vertical -> this is a gate
            hist[w] = hist.get(w, 0) + 1
    return hist


def _assert_gate_length(gds_json_path, expected_nm):
    """The checker under test. Every drawn gate must be exactly expected_nm."""
    hist = _drawn_gate_lengths(gds_json_path)
    assert hist, f"no vertical poly (gate) shapes found in {gds_json_path}"
    assert set(hist) == {expected_nm}, (
        f"drawn gate length(s) {sorted(hist)}nm != requested L={expected_nm}nm "
        f"in {gds_json_path}. If every gate is 150nm regardless of L, the "
        f"generator has gone back to ignoring the netlist channel length."
    )


def _run_align(tmp_path, length_nm):
    """Run the full flow on five_transistor_ota with L overridden, return the
    directory holding the generated primitives."""
    src = (EXAMPLES / "five_transistor_ota" / "five_transistor_ota.sp").read_text()
    work = tmp_path / f"L{length_nm}"
    work.mkdir(parents=True, exist_ok=True)
    (work / "five_transistor_ota.sp").write_text(
        src.replace("L=150e-9", f"L={length_nm}e-9")
    )
    shutil.copy(EXAMPLES / "five_transistor_ota" / "five_transistor_ota.const.json", work)

    subprocess.run(
        [sys.executable, shutil.which("schematic2layout.py"), ".",
         "-f", "five_transistor_ota.sp", "-s", "five_transistor_ota",
         "-p", str(PDK_DIR)],
        cwd=work, check=True, capture_output=True, timeout=1800,
    )
    prims = sorted((work / "2_primitives").glob("DP_NMOS*.gds.json"))
    assert prims, f"no DP_NMOS primitive generated at L={length_nm}"
    return prims[0]


@pytest.fixture(scope="module")
def layouts(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("L")
    return {L: _run_align(tmp, L) for L in L_VALUES_NM}


@pytest.mark.parametrize("length_nm", L_VALUES_NM)
def test_drawn_gate_length_matches_netlist(layouts, length_nm):
    """The drawn gate must physically equal the L asked for in the netlist."""
    _assert_gate_length(layouts[length_nm], length_nm)


def test_distinct_L_gives_distinct_geometry(layouts):
    """Two netlists differing ONLY in L must not produce identical geometry.

    This is the original bug stated directly: it used to print
    'POLYGON SETS IDENTICAL: True'.
    """
    short = _drawn_gate_lengths(layouts[150])
    long_ = _drawn_gate_lengths(layouts[1000])
    assert short != long_, (
        "L=150nm and L=1000nm produced identical gate geometry -- the "
        "generator is ignoring the netlist channel length again."
    )


def test_negative_control_detects_ignored_L(layouts):
    """NEGATIVE CONTROL.

    Hand the checker the NOMINAL 150nm layout but claim it was built for
    L=1000nm. That is precisely what the buggy generator produced, so the
    checker MUST reject it. If this test ever passes silently, the guard above
    has stopped being able to fail and is no longer protecting anything.
    """
    with pytest.raises(AssertionError, match="ignoring the netlist channel length|!= requested L"):
        _assert_gate_length(layouts[150], 1000)


def test_nominal_length_is_a_no_op(layouts):
    """L=150nm must still draw exactly 150nm gates, i.e. the fix must not
    perturb the nominal device that the rest of the PDK was tuned against."""
    assert set(_drawn_gate_lengths(layouts[150])) == {150}
