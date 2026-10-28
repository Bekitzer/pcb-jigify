#!/usr/bin/env python3

import tempfile
import subprocess
import sys
from pathlib import Path
import argparse
import cadquery as cq
from .jigs.testing import jig as testing
from .jigs.holding import jig as holding
from .jigs.settings import Settings
from . import inputs

KICAD_PCB = '.kicad_pcb'
KICAD_CLI = 'kicad-cli'
DXF_TOL = 1e-4

common_parser = argparse.ArgumentParser(add_help=False)

common_parser.add_argument('--registration-layer', help='Registration layer, either a KiCad layer name or a DXF file', type=str)
common_parser.add_argument('--registration-depth', help='How deep to drill the registration layer', type=float)

common_parser.add_argument('--margin', default=Settings.wallT, type=float)

common_parser.add_argument('--pcb-thickness', default=None, help=f'Thickness of the PCB (default: read from a STEP input, otherwise {Settings.pcbT})', type=float)
common_parser.add_argument('--pcb-fit', default=Settings.pcbFit, help='Fit of the PCB', type=float)

common_parser.add_argument('--output', help='Output file', type=str, required=True)

common_parser.add_argument('--dxf-tolerance', help='DXF edge consolidation tolerance', type=float, default=DXF_TOL)

common_parser.add_argument('--drills', help='Drill source for registration features: a DXF, a vector PDF drill drawing or a STEP file. '
                                            'Defaults to the holes of the input if it is a STEP file')
common_parser.add_argument('--registration-drill-diameter', help='Use drills of this diameter (mm) as registration features', type=float)
common_parser.add_argument('--registration-drill-tolerance', help='Drill diameter match tolerance (mm)', type=float, default=0.05)
common_parser.add_argument('--drills-pdf-scale', help='Scale of a PDF drill drawing (1 = 1:1, 2 = 2:1); deduced from the board outline if omitted', type=float)

common_parser.add_argument('file', help='PCB (KiCad or PCBA STEP) or edge cuts DXF file to process')

parser = argparse.ArgumentParser(
                    prog='pcb-jigify',
                    description='Generate PCB jigs')

subparsers = parser.add_subparsers()

holding_parser = subparsers.add_parser('holding', parents=[common_parser], help='Generate a jig for holding a PCB in place')

holding_parser.add_argument('--bottom-magnet-diameter', help='Diameter of the bottom magnet (hugging the surface)', type=float)
holding_parser.add_argument('--bottom-magnet-height', help='Height of the bottom magnet (hugging the surface)', type=float)

holding_parser.add_argument('--cut', action='store_true', help='Cut the jig in half', default=False)
holding_parser.add_argument('--part-basket', action='store_true', help='Add in a tiny part basket to store parts in during work')

testing_parser = subparsers.add_parser('testing', parents=[common_parser], help='Generate a jig for testing a PCB with probes (usually spring pins)')
testing_parser.add_argument('--testing-layer', help='Testing layer, either a KiCad layer name or a DXF file', required=True)
testing_parser.add_argument('--test-probe-diameter', help='Test probe diameter', required=True, type=float)
testing_parser.add_argument('--test-probe-length', help='Length of the test probe to hold onto', required=True, type=float)
testing_parser.add_argument('--side', help='Side of the board facing the testing fixture', default="top", choices=["top", "bottom"])

drills_parser = subparsers.add_parser('drills', help='List the drill diameters found in a STEP, DXF or PDF drill source')
drills_parser.add_argument('--drills', help='Drill source (DXF, vector PDF or STEP); defaults to the holes of a STEP input')
drills_parser.add_argument('--drills-pdf-scale', type=float, help='Scale of a PDF drill drawing (1 = 1:1)')
drills_parser.add_argument('--dxf-tolerance', type=float, default=DXF_TOL)
drills_parser.add_argument('file', help='PCB (KiCad or PCBA STEP) or edge cuts DXF file the drills belong to')

def kicad_export_dxf(file, layers, output):
    subprocess.run([
                   KICAD_CLI, 'pcb', 'export', 'dxf',
                   '--layers', ','.join(layers),
                   '--mode-multi', # Supress a deprecation notice
                   '-o', output,
                   '--ou', 'mm', file
    ])
    return output

def read_layers(file, registration_layer = None, testing_layer = None, dxf_tolerance=DXF_TOL):
    """Returns (outline, registration, testing, step_board); step_board is only set for STEP input."""
    if inputs.is_step(file):
        board = inputs.load_step(file)
        return (
            cq.Workplane("XY").add(board.outline),
            cq.importers.importDXF(registration_layer, tol=dxf_tolerance) if registration_layer is not None else None,
            cq.importers.importDXF(testing_layer, tol=dxf_tolerance) if testing_layer is not None else None,
            board,
        )
    read = read_layers_from_pcb if file.endswith(KICAD_PCB) else read_layers_from_dxf
    return (*read(file, registration_layer, testing_layer, dxf_tolerance), None)

def registration_wires(outline, registration_layer, registration_depth, step_board, args):
    """Combines the registration layer and any selected drills into registration wires."""
    wires = list(registration_layer.wires().vals()) if registration_layer is not None else []

    if args['registration_drill_diameter'] is not None:
        if args['drills'] is not None:
            drills = inputs.load_drills(args['drills'], outline.vals(), args['dxf_tolerance'], args['drills_pdf_scale'])
        elif step_board is not None:
            drills = step_board.drills
        else:
            sys.exit('--registration-drill-diameter needs a STEP input or --drills')
        found = inputs.select_drills(drills, args['registration_drill_diameter'], args['registration_drill_tolerance'])
        if not found:
            sys.exit(f'No drills of {args["registration_drill_diameter"]} mm found. Available diameters (mm: count): {inputs.drill_summary(drills)}')
        wires += [d.wire() for d in found]
    elif args['drills'] is not None:
        sys.exit('--drills needs --registration-drill-diameter (see `pcb-jigify drills` for the available diameters)')

    return wires or None

def read_layers_from_dxf(file, registration_layer = None, testing_layer = None, dxf_tolerance=DXF_TOL):
    return (
        cq.importers.importDXF(file, tol=dxf_tolerance),
        cq.importers.importDXF(registration_layer, tol=dxf_tolerance) if registration_layer is not None else None,
        cq.importers.importDXF(testing_layer, tol=dxf_tolerance) if testing_layer is not None else None,
    )

def read_layers_from_pcb(file, registration_layer = None, testing_layer = None, dxf_tolerance=DXF_TOL):
    with tempfile.TemporaryDirectory() as dir:
        export_layers = ["Edge.Cuts", registration_layer, testing_layer]
        kicad_export_dxf(file, [l for l in export_layers if l is not None], dir)
        layers = read_layers_from_dxf(
            *[f"{dir}/{Path(file).stem}-{l.replace('.', '_')}.dxf" if l is not None else None for l in export_layers],
            dxf_tolerance
        )
    return layers


def resolve_thickness(pcb_thickness, step_board):
    if pcb_thickness is not None:
        return pcb_thickness
    return step_board.thickness if step_board is not None else Settings.pcbT


def holding_main(file,
                 pcb_thickness,
                 output,
                 registration_layer,
                 registration_depth,
                 bottom_magnet_diameter,
                 bottom_magnet_height,
                 cut,
                 part_basket,
                 pcb_fit,
                 dxf_tolerance,
                 **rest):
    file, registration_layer, _, step_board = read_layers(file, registration_layer, dxf_tolerance=dxf_tolerance)
    registration = registration_wires(file, registration_layer, registration_depth, step_board,
                                      dict(rest, dxf_tolerance=dxf_tolerance))
    pcb_thickness = resolve_thickness(pcb_thickness, step_board)

    if (bottom_magnet_diameter is None) != (bottom_magnet_height is None):
        holding_parser.print_help()
        print('\n--bottom-magnet-diameter and --bottom-magnet-height both need to be specified')
        sys.exit(1)

    if (registration is None) != (registration_depth is None):
        holding_parser.print_help()
        print('\n--registration-layer and --registration-depth both need to be specified')
        sys.exit(1)

    j = holding(
        file.wires(),
        registration = registration,
        registrationDepth=registration_depth,
        surfaceMagnet=(bottom_magnet_diameter, bottom_magnet_height) if bottom_magnet_diameter is not None else None,
        cut=cut,
        partBasket=[10, 10, pcb_thickness] if part_basket else None,
        pcbT = pcb_thickness,
        pcbFit=pcb_fit,
    )
    j.export(output)

holding_parser.set_defaults(func=holding_main)

def testing_main(file,
                 registration_layer,
                 registration_depth,
                 testing_layer,
                 pcb_thickness,
                 test_probe_diameter,
                 test_probe_length,
                 side,
                 output,
                 pcb_fit,
                 dxf_tolerance,
                 **rest):

    if registration_depth is None and (registration_layer is not None or rest.get('registration_drill_diameter') is not None):
        sys.exit('--registration-depth needs to be specified together with registration features')

    file, registration_layer, testing_layer, step_board = read_layers(file, registration_layer, testing_layer, dxf_tolerance)
    registration = registration_wires(file, registration_layer, registration_depth, step_board,
                                      dict(rest, dxf_tolerance=dxf_tolerance))
    pcb_thickness = resolve_thickness(pcb_thickness, step_board)

    j = testing(
        file.wires(),
        testPoint = ( test_probe_diameter, test_probe_length ),
        registration = registration,
        registrationDepth = registration_depth,
        pcbT = pcb_thickness,
        pcbFit = pcb_fit,
        testPoints = testing_layer.wires(),

        side = side,
    )
    j.export(output)

testing_parser.set_defaults(func=testing_main)


def drills_main(file, drills, dxf_tolerance, drills_pdf_scale, **rest):
    outline, _, _, step_board = read_layers(file, dxf_tolerance=dxf_tolerance)
    if drills is not None:
        found = inputs.load_drills(drills, outline.vals(), dxf_tolerance, drills_pdf_scale)
    elif step_board is not None:
        found = step_board.drills
    else:
        sys.exit('Pass --drills, or a STEP file as the input')
    for diameter, count in inputs.drill_summary(found).items():
        print(f'{diameter:g} mm: {count}')

drills_parser.set_defaults(func=drills_main)


def main():
    args = parser.parse_args()

    if 'func' not in args:
        parser.print_help()
        sys.exit(1)
    args.func(**vars(args))


if __name__ == "__main__":
    main()
