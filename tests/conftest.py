"""Shared helpers for the sky130 PDK regression tests.

The tests in this directory all have the same shape, because the bugs they
guard against all have the same shape: a parameter the netlist states, silently
replaced by a value the generator assumed. Catching one of those means running
the real flow and then MEASURING THE EMITTED POLYGONS -- a parameter that is
computed correctly and then not drawn is the same bug one layer down.

So this module offers exactly two things:

    run_align()            build a netlist variant through the real flow
    drawn_extents()        histogram the drawn size of shapes on one GDS layer

which is enough to write the next such guard in a few lines.
"""
import pathlib
import re
import shutil
import subprocess
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
PDK_DIR = ROOT / "SKY130_PDK"
EXAMPLES = ROOT / "examples"

# GDS (layer, datatype) of the shapes each netlist parameter is supposed to
# control. Poly gates are drawn at L; difftap source/drain strips at W.
POLY = (66, 20)
DIFFTAP = (65, 20)


def run_align(work_dir, example, substitutions, subckt=None, timeout=3600):
    """Run the full flow on `example` with `substitutions` applied to its .sp.

    `substitutions` is a list of (regex, replacement) pairs applied to the
    netlist text. Returns the 2_primitives directory.
    """
    subckt = subckt or example
    src = (EXAMPLES / example / f"{example}.sp").read_text()
    for pattern, replacement in substitutions:
        src, n = re.subn(pattern, replacement, src, flags=re.IGNORECASE)
        assert n, f"substitution {pattern!r} matched nothing in {example}.sp"

    work_dir = pathlib.Path(work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)
    (work_dir / f"{example}.sp").write_text(src)
    const = EXAMPLES / example / f"{example}.const.json"
    if const.is_file():
        shutil.copy(const, work_dir)

    subprocess.run(
        [sys.executable, shutil.which("schematic2layout.py"), ".",
         "-f", f"{example}.sp", "-s", subckt, "-p", str(PDK_DIR)],
        cwd=work_dir, check=True, capture_output=True, timeout=timeout,
    )
    return work_dir / "2_primitives"


def run_align_expecting_failure(work_dir, example, substitutions, subckt=None):
    """Same, but the flow is expected to reject the netlist.

    Returns stdout and stderr joined: ALIGN installs a logging handler on
    stdout and prints the traceback there, so the assertion text that names the
    offending device does NOT reliably land on stderr.
    """
    with pytest.raises(subprocess.CalledProcessError) as excinfo:
        run_align(work_dir, example, substitutions, subckt=subckt, timeout=600)
    err = excinfo.value
    return (err.stdout or b"").decode("utf-8", "replace") + \
           (err.stderr or b"").decode("utf-8", "replace")


def drawn_extents(gds_json_path, layer_datatype, orientation):
    """Return {size_nm: count} for shapes on one GDS layer.

    `orientation` selects which of the two extents is reported and which shapes
    are counted, because several logical layers share one GDS layer number:

        'vertical'   tall shapes; reports WIDTH   (poly gates, drawn at L)
        'horizontal' wide shapes; reports HEIGHT  (difftap strips, drawn at W)
    """
    import json
    layer, datatype = layer_datatype
    with open(gds_json_path) as fp:
        data = json.load(fp)
    hist = {}
    for lib in data["bgnlib"]:
        for struct in lib["bgnstr"]:
            for el in struct["elements"]:
                if el.get("type") != "boundary":
                    continue
                if (el.get("layer"), el.get("datatype")) != (layer, datatype):
                    continue
                xs, ys = el["xy"][0::2], el["xy"][1::2]
                w, h = max(xs) - min(xs), max(ys) - min(ys)
                if orientation == "vertical" and h >= w:
                    hist[w] = hist.get(w, 0) + 1
                elif orientation == "horizontal" and w >= h:
                    hist[h] = hist.get(h, 0) + 1
    return hist
