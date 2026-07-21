"""Regression guard: the MOS generator must honour the netlist width W.

sky130 is a planar BULK process -- its own design_info says pdk_type "Bulk" and
mos.py never draws a single shape on the Fin layer. Historically gen_param.py
nevertheless asserted `W_nm % Fin_pitch == 0` with a fin pitch of 210nm and then
converted W into a fin COUNT, so any width that was not a multiple of 210nm was
rejected outright:

    AssertionError: Width of device M1 in SCM_NMOS_48371519 should be
                    multiple of fin pitch:210

A real design asking for W = 1um could not be built. Nobody noticed because all
six shipped examples use w=10.5e-7 / 21e-7 / 840e-9 / 630e-9 / 420e-9, every one
of them an exact multiple of 210 -- examples/umich_test_case/umich_test_case.sp
even carries the comment "*ALIGN (sizing): Width of device should be multiple of
fin pitch (210e-9)" recording that the netlist was rewritten to fit the tool.

These tests fail if that ever comes back.

NEGATIVE CONTROL
----------------
`test_negative_control_detects_wrong_width` proves the assertion used by
`test_drawn_width_matches_netlist` is actually capable of failing: it feeds the
checker a layout drawn at the nominal 1050nm while claiming 1000nm was asked
for, and requires the checker to reject it. A guard that cannot fail is
worthless.

Run:  pytest -v tests/test_drawn_width.py
"""
import copy
import importlib.util
import json
import pathlib
import types

import pytest

from conftest import DIFFTAP, PDK_DIR, drawn_extents, run_align, run_align_expecting_failure

FIN_PITCH = 210

# W values in nm to exercise.
#   1050 is the shipped nominal (5 x 210) and MUST be a strict no-op;
#   420 is the minimum drawable width (and, coincidentally, 2 x 210);
#   1000 and 1500 are NOT multiples of 210 -- these could not be built at all
#   before this fix.
W_VALUES_NM = [420, 1000, 1050, 1500]
NOMINAL_NM = 1050


def _assert_drawn_width(gds_json_path, expected_nm):
    """The checker under test. Every drawn difftap strip must be exactly W."""
    hist = drawn_extents(gds_json_path, DIFFTAP, "horizontal")
    assert hist, f"no horizontal difftap (source/drain) shapes in {gds_json_path}"
    assert set(hist) == {expected_nm}, (
        f"drawn width(s) {sorted(hist)}nm != requested W={expected_nm}nm in "
        f"{gds_json_path}. If the width has been snapped to a multiple of the "
        f"{FIN_PITCH}nm fin pitch, the generator is quantising a planar bulk "
        f"process as if it had fins again."
    )


@pytest.fixture(scope="module")
def layouts(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("W")
    out = {}
    for width_nm in W_VALUES_NM:
        prims = run_align(
            tmp / f"W{width_nm}", "five_transistor_ota",
            [(r"w=10\.5e-7", f"w={width_nm}e-9")],
        )
        found = sorted(prims.glob("DP_NMOS*.gds.json"))
        assert found, f"no DP_NMOS primitive generated at W={width_nm}"
        out[width_nm] = found[0]
    return out


@pytest.mark.parametrize("width_nm", W_VALUES_NM)
def test_drawn_width_matches_netlist(layouts, width_nm):
    """The drawn source/drain diffusion must physically equal the W asked for."""
    _assert_drawn_width(layouts[width_nm], width_nm)


@pytest.mark.parametrize("width_nm", [w for w in W_VALUES_NM if w % FIN_PITCH])
def test_width_off_the_fin_pitch_is_buildable(layouts, width_nm):
    """The headline case: a width that is NOT a multiple of the fin pitch.

    This is what the old assertion rejected outright, so reaching a layout at
    all is the thing being tested; the exact geometry is checked above.
    """
    assert width_nm % FIN_PITCH != 0, "this test is pointless on a fin multiple"
    assert layouts[width_nm].is_file()


def test_multiple_of_fin_pitch_is_unchanged(layouts):
    """NOT OPTIONAL. Every shipped example uses a width that IS a multiple of
    210nm, so this is the regression the fix could plausibly cause."""
    _assert_drawn_width(layouts[NOMINAL_NM], NOMINAL_NM)


def test_distinct_W_gives_distinct_geometry(layouts):
    """Two netlists differing ONLY in W must not produce the same geometry.

    1000nm and 1050nm are the interesting pair: they occupy the SAME number of
    210nm rows (ceil(1000/210) == ceil(1050/210) == 5), so a generator that
    still drew row_count * fin_pitch would emit identical diffusion for both.
    """
    narrow = drawn_extents(layouts[1000], DIFFTAP, "horizontal")
    wide = drawn_extents(layouts[NOMINAL_NM], DIFFTAP, "horizontal")
    assert narrow != wide, (
        "W=1000nm and W=1050nm produced identical diffusion geometry -- the "
        "generator is drawing the fin-row allocation instead of the width."
    )


def test_negative_control_detects_wrong_width(layouts):
    """NEGATIVE CONTROL.

    Hand the checker the nominal 1050nm layout but claim W=1000nm was asked
    for. A generator that snaps W up to the next fin multiple produces exactly
    this, so the checker MUST reject it. If this test ever passes silently the
    guard above has stopped being able to fail.
    """
    with pytest.raises(AssertionError, match="quantising a planar bulk|!= requested W"):
        _assert_drawn_width(layouts[NOMINAL_NM], 1000)


# --------------------------------------------------------------------------
# Bounds. Every number here is derived from the PDK's own data -- see
# gen_param._drawn_width_bounds -- so these also guard the derivation.
# --------------------------------------------------------------------------

@pytest.mark.parametrize("width_nm,message", [
    (410,  "minimum drawn width"),      # below sky130 diff/tap.2, 420nm
    (2530, "maximum drawn width"),      # beyond unit_size_mos * Fin_pitch
    (1005, "multiple of 10nm"),         # off the drawable 10nm quantum
])
def test_out_of_range_width_is_rejected(tmp_path, width_nm, message):
    """Honouring W does not mean accepting any W. A width the process cannot
    manufacture must still be refused, by name, before any layout is drawn."""
    stderr = run_align_expecting_failure(
        tmp_path / f"W{width_nm}", "five_transistor_ota",
        [(r"w=10\.5e-7", f"w={width_nm}e-9")],
    )
    assert message in stderr, f"expected {message!r} in:\n{stderr[-2000:]}"


# --------------------------------------------------------------------------
# The FinFET branch. ALIGN PDKs copy this gen_param.py, and on a genuine fin
# process W really is a fin count, so that path must keep quantising.
# --------------------------------------------------------------------------

def _load_gen_param(pdk_dir):
    spec = importlib.util.spec_from_file_location("gen_param_under_test", pdk_dir / "gen_param.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _fake_mos_subckt(width_m):
    """Minimal stand-in for the SubCircuit gen_param() reads."""
    element = types.SimpleNamespace(
        name="M1",
        model="sky130_fd_pr__nfet_01v8",
        parameters={"W": str(width_m), "L": "1.5E-07", "NF": "2", "M": "1"},
    )
    return types.SimpleNamespace(
        name="SCM_NMOS_TEST", elements=[element], generator={"name": "MOS"},
    )


@pytest.fixture
def finfet_pdk(tmp_path):
    """A copy of this PDK relabelled as a fin process."""
    data = json.loads((PDK_DIR / "layers.json").read_text())
    data["design_info"] = copy.deepcopy(data["design_info"])
    data["design_info"]["pdk_type"] = "FinFET"
    pdk_dir = tmp_path / "FINFET_PDK"
    pdk_dir.mkdir()
    (pdk_dir / "layers.json").write_text(json.dumps(data))
    (pdk_dir / "gen_param.py").write_bytes((PDK_DIR / "gen_param.py").read_bytes())
    return pdk_dir


def test_finfet_path_still_quantises_to_fin_pitch(finfet_pdk):
    """On a fin process a width off the fin pitch must still be rejected."""
    module = _load_gen_param(finfet_pdk)
    with pytest.raises(AssertionError, match="multiple of fin pitch"):
        module.gen_param(_fake_mos_subckt(1000e-9), {}, finfet_pdk)


def test_finfet_path_still_counts_fins(finfet_pdk):
    """...and a width on the fin pitch must still become that many fins."""
    module = _load_gen_param(finfet_pdk)
    primitives = {}
    module.gen_param(_fake_mos_subckt(1050e-9), primitives, finfet_pdk)
    assert primitives, "no primitive emitted"
    assert {p["value"] for p in primitives.values()} == {1050 // FIN_PITCH}


def test_bulk_path_accepts_what_the_finfet_path_rejects():
    """The two branches must actually differ -- otherwise the pdk_type test in
    gen_param.py is dead code and this whole file is testing one path twice."""
    module = _load_gen_param(PDK_DIR)
    assert json.loads((PDK_DIR / "layers.json").read_text())["design_info"]["pdk_type"] == "Bulk"
    primitives = {}
    module.gen_param(_fake_mos_subckt(1000e-9), primitives, PDK_DIR)
    assert primitives, "W=1000nm rejected on the bulk path"
