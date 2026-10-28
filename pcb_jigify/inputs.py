"""Alternative board inputs: PCBA STEP files and drill sources (STEP, DXF, PDF)."""

import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import cadquery as cq

STEP_EXTS = ('.step', '.stp')
DXF_EXT = '.dxf'
PDF_EXT = '.pdf'

# Circles and shapes are matched to this tolerance (mm).
_CIRCLE_TOL = 1e-3


@dataclass
class Drill:
    x: float
    y: float
    diameter: float

    def wire(self) -> cq.Wire:
        return cq.Wire.makeCircle(self.diameter / 2, cq.Vector(self.x, self.y, 0), cq.Vector(0, 0, 1))


@dataclass
class Board:
    outline: list
    thickness: float
    drills: list = field(default_factory=list)


def is_step(path: str) -> bool:
    return str(path).lower().endswith(STEP_EXTS)


def circle_of_wire(wire: cq.Wire) -> Optional[Drill]:
    """Returns the circle described by a wire, or None if it isn't one."""
    edges = wire.Edges()
    if not edges or not wire.IsClosed() or any(e.geomType() != 'CIRCLE' for e in edges):
        return None
    center, radius = edges[0].arcCenter(), edges[0].radius()
    for e in edges[1:]:
        if (e.arcCenter() - center).Length > _CIRCLE_TOL or abs(e.radius() - radius) > _CIRCLE_TOL:
            return None
    return Drill(center.x, center.y, 2 * radius)


def _bbox_area(shape):
    bb = shape.BoundingBox()
    return (bb.xmax - bb.xmin) * (bb.ymax - bb.ymin)


def load_step(path: str) -> Board:
    """Extracts the bare PCB from a PCBA STEP file.

    The board is assumed to be Z-up, which is what KiCad, Altium and friends
    export. It is picked as the largest, flat solid; components are ignored.
    Its outline, thickness and round holes (drills) are returned, translated
    so that the board's top face lies on Z=0.
    """
    solids = cq.importers.importStep(path).solids().vals()
    if not solids:
        raise ValueError(f'{path}: no solids found in STEP file')

    footprint = max(_bbox_area(s) for s in solids)
    # Solder mask or silkscreen layers can share the board's footprint; the
    # actual board is the bulkiest of them.
    board = max((s for s in solids if _bbox_area(s) >= 0.99 * footprint), key=lambda s: s.Volume())

    bb = board.BoundingBox()
    top = [
        f for f in board.Faces()
        if f.geomType() == 'PLANE' and f.normalAt().z > 0.999 and abs(f.Center().z - bb.zmax) < 1e-3
    ]
    if not top:
        raise ValueError(f'{path}: could not find the PCB top face; is the board Z-up?')

    shift = cq.Vector(0, 0, -bb.zmax)
    outlines = [f.outerWire().translate(shift) for f in top]
    outlines.sort(key=_bbox_area, reverse=True)

    drills = []
    for f in top:
        for inner in f.innerWires():
            d = circle_of_wire(inner.translate(shift))
            if d is not None:
                drills.append(d)

    return Board(outline=[outlines[0]], thickness=bb.zmax - bb.zmin, drills=drills)


def load_dxf_drills(path: str, tol: float) -> list:
    wires = cq.importers.importDXF(path, tol=tol).wires().vals()
    return [d for d in map(circle_of_wire, wires) if d is not None]


def _chains(items):
    """Splits PyMuPDF drawing items into runs of connected segments."""
    chain, last = [], None
    for item in items:
        if item[0] == 're':
            if chain:
                yield chain
            yield [item]
            chain, last = [], None
            continue
        if chain and abs(item[1] - last) > 1e-3:
            yield chain
            chain = []
        chain.append(item)
        last = item[-1]
    if chain:
        yield chain


def _chain_points(chain):
    pts = []
    for item in chain:
        if item[0] == 're':
            r = item[1]
            pts += [(r.x0, r.y0), (r.x1, r.y0), (r.x1, r.y1), (r.x0, r.y1)]
        else:
            pts += [(p.x, p.y) for p in item[1:] if hasattr(p, 'x')]
    return pts


def _chain_circle(chain):
    """Returns (cx, cy, d) in PDF points if the chain is a closed circle."""
    if chain[0][0] == 're' or len(chain) < 4:
        return None
    if abs(chain[0][1] - chain[-1][-1]) > 1e-2:
        return None
    kinds = {i[0] for i in chain}
    if kinds == {'c'}:
        pts = [(i[1].x, i[1].y) for i in chain]  # on-curve points
    elif kinds == {'l'} and len(chain) >= 8:
        pts = [(i[1].x, i[1].y) for i in chain]
    else:
        return None
    cx = sum(p[0] for p in pts) / len(pts)
    cy = sum(p[1] for p in pts) / len(pts)
    radii = [math.hypot(p[0] - cx, p[1] - cy) for p in pts]
    r = sum(radii) / len(radii)
    if r <= 0 or max(abs(x - r) for x in radii) > 0.02 * r:
        return None
    return cx, cy, 2 * r


def load_pdf_drills(path: str, outline, scale: Optional[float] = None, page: int = 0) -> list:
    """Reads drills from a vector PDF drill drawing.

    Round holes are recognised as closed circles. A PDF has no board origin,
    so the board outline drawn on the page is located (by matching its shape
    against `outline`, the board outline already loaded from another input)
    and used to place the holes in the board's coordinate system.

    `scale` is the drawing scale (1.0 = printed 1:1, 2.0 = 2:1). When it's
    omitted it is deduced from the drawn outline.
    """
    import pymupdf

    doc = pymupdf.open(path)
    drawings = doc[page].get_drawings()

    circles, closed = [], []
    for drawing in drawings:
        for chain in _chains(drawing['items']):
            c = _chain_circle(chain)
            if c is not None:
                circles.append(c)
                continue
            pts = _chain_points(chain)
            if len(pts) < 3:
                continue
            xs, ys = [p[0] for p in pts], [p[1] for p in pts]
            if chain[0][0] == 're' or abs(chain[0][1] - chain[-1][-1]) < 1e-2:
                closed.append((min(xs), min(ys), max(xs), max(ys)))

    if not circles:
        raise ValueError(f'{path}: no round holes found on page {page + 1}; is it a vector PDF?')

    bb = cq.Compound.makeCompound(outline).BoundingBox()
    width, height = bb.xmax - bb.xmin, bb.ymax - bb.ymin

    pt_mm = 25.4 / 72
    match = None
    for x0, y0, x1, y1 in sorted(closed, key=lambda b: (b[2] - b[0]) * (b[3] - b[1]), reverse=True):
        w, h = x1 - x0, y1 - y0
        if w <= 0 or h <= 0:
            continue
        s = scale * pt_mm if scale else width / w
        if abs(w * s - width) <= 0.03 * width and abs(h * s - height) <= 0.03 * height:
            match = (x0, y1, s)  # PDF y points down, so anchor on the bottom edge
            break
    if match is None:
        raise ValueError(
            f'{path}: could not find the board outline ({width:.2f} x {height:.2f} mm) '
            'in the PDF. Include the board edge in the drill drawing, and pass '
            '--drills-pdf-scale if the drawing is not 1:1.'
        )

    x0, y1, s = match
    return [Drill(bb.xmin + (cx - x0) * s, bb.ymin + (y1 - cy) * s, d * s) for cx, cy, d in circles]


def select_drills(drills, diameter: float, tolerance: float) -> list:
    return [d for d in drills if abs(d.diameter - diameter) <= tolerance]


def drill_summary(drills) -> dict:
    counts = {}
    for d in drills:
        key = round(d.diameter, 2)
        counts[key] = counts.get(key, 0) + 1
    return dict(sorted(counts.items()))


def load_drills(path: str, outline, tol: float, scale: Optional[float] = None) -> list:
    suffix = Path(path).suffix.lower()
    if suffix == DXF_EXT:
        return load_dxf_drills(path, tol)
    if suffix == PDF_EXT:
        return load_pdf_drills(path, outline, scale)
    if suffix in STEP_EXTS:
        return load_step(path).drills
    raise ValueError(f'{path}: unsupported drill file type (expected .dxf, .pdf or .step/.stp)')
