"""Generate the synthetic demonstration corpus in ops/demo/corpus/.

Run once; the output is committed so every machine seeds the same bytes:

    python ops/demo/make_corpus.py            # writes only the files that are missing
    python ops/demo/make_corpus.py --force    # rewrites every file (new bytes: re-seeds!)

Files that already exist are left alone by default: the seed recognises a document by
the SHA-256 of its bytes, so regenerating a file an installation has already ingested
would file it a second time.

Every document is invented, marked "SYNTHETIC DEMONSTRATION DATA" on every page, and
belongs to a fictional plant ("Konkan Process Industries, Unit 7"). The numbers are
internally consistent on purpose -- the E-101 report's remaining-life figure really is
(9.2 - 8.4) / 0.25 -- so grounding checks and the calculator have something true to
find. Scanned documents are rendered, rotated, blurred and noised, and carry a stamp and
a hand-drawn signature, so OCR and the vision model have real work to do.

The classifications and ACLs in manifest.yaml are chosen so the three seeded identities
see visibly different subsets of the same query (ADR-0001 §Q7): the incident report is
denied to one engineer by classification and to the other by ACL.

The workbench scenario (a report on lathe L-1) adds a machine shop, a procurement desk,
company policies -- Policy 1 in two revisions a month apart, so "what changed in policy 1
since last month" has a real, citable answer -- and an offline reference library that
stands in for web search. Its numbers are consistent too: L-1's 12-month repair cost is
31.2% of its replacement cost (3.9 / 12.5), which is under Policy 1's threshold last
month (40%) and over it today (30%).

Needs reportlab, pypdfium2 and Pillow -- tooling for this script only, not runtime
dependencies of anything under packages/.
"""

from __future__ import annotations

import math
import random
from io import BytesIO
from pathlib import Path
from typing import Sequence

import pypdfium2 as pdfium
from PIL import Image, ImageDraw, ImageFilter, ImageFont
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.pdfgen.canvas import Canvas

OUT = Path(__file__).resolve().parent / "corpus"
PLANT = "KONKAN PROCESS INDUSTRIES  -  UNIT 7"
FOOTER = "SYNTHETIC DEMONSTRATION DATA - fictional plant, generated for the Citadel demonstration"
FONT_DIR = Path("/usr/share/fonts/truetype/dejavu")

Line = tuple[str, str]  # (style, text): style in h1, h2, b (bold), t (text), m (mono), gap


def _draw_page(canvas: Canvas, lines: Sequence[Line], page: int, pages: int, marking: str) -> None:
    width, height = A4
    canvas.setFillColor(colors.HexColor("#1d2b3a"))
    canvas.rect(0, height - 22 * mm, width, 22 * mm, stroke=0, fill=1)
    canvas.setFillColor(colors.white)
    canvas.setFont("Helvetica-Bold", 12)
    canvas.drawString(18 * mm, height - 13 * mm, PLANT)
    canvas.setFont("Helvetica-Bold", 10)
    canvas.drawRightString(width - 18 * mm, height - 13 * mm, marking)
    y = height - 34 * mm
    for style, text in lines:
        if style == "gap":
            y -= 4 * mm
            continue
        font, size, lead = {
            "h1": ("Helvetica-Bold", 15, 8),
            "h2": ("Helvetica-Bold", 11.5, 7),
            "b": ("Helvetica-Bold", 10, 5.6),
            "t": ("Helvetica", 10, 5.6),
            "m": ("Courier", 9.5, 5.2),
        }[style]
        canvas.setFillColor(colors.black)
        canvas.setFont(font, size)
        canvas.drawString(18 * mm, y, text)
        y -= lead * mm
    canvas.setFont("Helvetica", 7.5)
    canvas.setFillColor(colors.HexColor("#555555"))
    canvas.drawString(18 * mm, 10 * mm, FOOTER)
    canvas.drawRightString(width - 18 * mm, 10 * mm, f"{marking}  |  page {page} of {pages}")


def born_digital(path: Path, pages: Sequence[Sequence[Line]], marking: str) -> None:
    # invariant=1: no creation timestamp or random document id, so the same lines always
    # produce the same bytes (and the same digest the seed recognises documents by).
    canvas = Canvas(str(path), pagesize=A4, invariant=1)
    canvas.setTitle(path.stem)
    for index, lines in enumerate(pages, start=1):
        _draw_page(canvas, lines, index, len(pages), marking)
        canvas.showPage()
    canvas.save()


def _font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    name = "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"
    return ImageFont.truetype(str(FONT_DIR / name), size)


def _stamp(image: Image.Image, center: tuple[int, int], lines: Sequence[str], colour: tuple[int, int, int]) -> None:
    layer = Image.new("RGBA", image.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    cx, cy = center
    radius = 118
    draw.ellipse((cx - radius, cy - radius, cx + radius, cy + radius), outline=colour + (210,), width=7)
    draw.ellipse((cx - radius + 16, cy - radius + 16, cx + radius - 16, cy + radius - 16), outline=colour + (190,), width=3)
    for i, text in enumerate(lines):
        font = _font(30 if i == 0 else 24, bold=True)
        box = draw.textbbox((0, 0), text, font=font)
        draw.text((cx - (box[2] - box[0]) / 2, cy - 48 + i * 36), text, font=font, fill=colour + (220,))
    layer = layer.rotate(-12, center=center, resample=Image.Resampling.BICUBIC)
    image.paste(layer, (0, 0), layer)


def _signature(image: Image.Image, origin: tuple[int, int], seed: int) -> None:
    rng = random.Random(seed)
    draw = ImageDraw.Draw(image)
    x0, y0 = origin
    points = []
    for step in range(90):
        t = step / 89
        x = x0 + t * 330
        y = y0 + math.sin(t * 13 + rng.random()) * 18 * (1 - t * 0.4) + math.sin(t * 41) * 6
        points.append((x, y))
    draw.line(points, fill=(20, 30, 110), width=4, joint="curve")
    draw.line([(x0 + 40, y0 + 26), (x0 + 300, y0 + 20)], fill=(20, 30, 110), width=3)


def scanned(
    path: Path,
    pages: Sequence[Sequence[Line]],
    marking: str,
    *,
    stamp: Sequence[str] | None = None,
    stamp_at: tuple[float, float] = (0.72, 0.46),
    signature_at: tuple[float, float] | None = (0.48, 0.38),
    seed: int = 7,
) -> None:
    """Lay the pages out as a real PDF, then turn each into a degraded page image."""
    buffer = BytesIO()
    canvas = Canvas(buffer, pagesize=A4)
    for index, lines in enumerate(pages, start=1):
        _draw_page(canvas, lines, index, len(pages), marking)
        canvas.showPage()
    canvas.save()
    rng = random.Random(seed)
    document = pdfium.PdfDocument(buffer.getvalue())
    images: list[Image.Image] = []
    for index in range(len(document)):
        bitmap = document[index].render(scale=150 / 72)
        image = bitmap.to_pil().convert("RGB")
        last = index == len(document) - 1
        if last and signature_at is not None:
            _signature(image, (int(image.width * signature_at[0]), int(image.height * signature_at[1])), seed + index)
        if last and stamp:
            _stamp(image, (int(image.width * stamp_at[0]), int(image.height * stamp_at[1])), stamp, (180, 28, 36))
        image = image.rotate(rng.uniform(-0.8, 0.8), resample=Image.Resampling.BICUBIC, expand=False, fillcolor=(255, 255, 255))
        image = image.filter(ImageFilter.GaussianBlur(0.6))
        pixels = image.load()
        assert pixels is not None
        for _ in range(image.width * image.height // 90):
            x, y = rng.randrange(image.width), rng.randrange(image.height)
            shade = rng.randrange(150, 235)
            pixels[x, y] = (shade, shade, shade)
        images.append(image.convert("L").convert("RGB"))
    images[0].save(path, "PDF", resolution=150, save_all=True, append_images=images[1:], quality=72)


def nameplate(path: Path) -> None:
    """A photographed equipment nameplate -- the image-understanding case (target D)."""
    rng = random.Random(3)
    image = Image.new("RGB", (1400, 900), (96, 101, 108))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((120, 90, 1280, 810), radius=28, fill=(196, 199, 196), outline=(60, 60, 60), width=6)
    for cx, cy in ((170, 140), (1230, 140), (170, 760), (1230, 760)):
        draw.ellipse((cx - 16, cy - 16, cx + 16, cy + 16), fill=(120, 120, 120), outline=(40, 40, 40), width=3)
    rows = [
        ("KONKAN PROCESS INDUSTRIES - UNIT 7", True, 40),
        ("PRESSURE VESSEL   V-204", True, 46),
        ("MFR SERIAL No.   KPI-PV-2011-0448", False, 34),
        ("YEAR BUILT   2011        CODE   ASME VIII DIV 1", False, 34),
        ("MAWP   16.0 barg  @  220 C", True, 36),
        ("HYDRO TEST   24.0 barg", False, 34),
        ("MDMT   -10 C      CORR. ALLOW.   3.0 mm", False, 34),
        ("SHELL  SA-516 GR 70    THK  14.0 mm", False, 34),
        ("SYNTHETIC DEMONSTRATION DATA", False, 24),
    ]
    y = 150
    for text, bold, size in rows:
        font = _font(size, bold=bold)
        box = draw.textbbox((0, 0), text, font=font)
        draw.text((700 - (box[2] - box[0]) / 2, y), text, font=font, fill=(25, 25, 28))
        y += size + 34
    image = image.rotate(-3.5, resample=Image.Resampling.BICUBIC, fillcolor=(70, 74, 80))
    image = image.filter(ImageFilter.GaussianBlur(1.0))
    pixels = image.load()
    assert pixels is not None
    for _ in range(image.width * image.height // 40):
        x, y2 = rng.randrange(image.width), rng.randrange(image.height)
        r, g, b = pixels[x, y2]
        delta = rng.randrange(-28, 28)
        pixels[x, y2] = (max(0, min(255, r + delta)), max(0, min(255, g + delta)), max(0, min(255, b + delta)))
    image.save(path, "PNG", optimize=True)


E101_P1: list[Line] = [
    ("h1", "MECHANICAL INTEGRITY - INSPECTION REPORT"),
    ("t", "Report No: IR-2026-0147          Date of inspection: 12 Aug 2026"),
    ("t", "Equipment tag: E-101             Description: Shell & tube heat exchanger (crude preheat)"),
    ("t", "Inspection type: Turnaround internal inspection + UT thickness survey"),
    ("t", "Design code: TEMA R / ASME VIII Div. 1      Shell material: SA-516 Gr. 70"),
    ("t", "Nominal shell thickness: 12.0 mm   Minimum required thickness (t-min): 8.4 mm"),
    ("t", "Corrosion allowance: 3.0 mm   Service: crude oil, 180 C, 14 barg"),
    ("gap", ""),
    ("h2", "1. UT THICKNESS READINGS (SHELL)"),
    ("m", "CML     LOCATION                 READING (mm)"),
    ("m", "CML-1   Shell top, inlet end         11.4"),
    ("m", "CML-2   Shell bottom                 10.1"),
    ("m", "CML-3   Near N2 nozzle                9.2"),
    ("m", "CML-4   Shell mid-span               10.8"),
    ("m", "CML-5   Outlet end                   11.1"),
    ("m", "CML-6   Bottom, 6 o'clock             9.6"),
    ("gap", ""),
    ("h2", "2. VISUAL OBSERVATIONS"),
    ("t", "- Localised pitting on the shell bottom near the N2 nozzle, maximum pit depth 1.1 mm."),
    ("t", "- Tube sheet: minor erosion at tube inlets; 14 tubes previously plugged."),
    ("t", "- Gasket seating surface at the channel flange is satisfactory."),
    ("t", "- External coating intact; no corrosion under insulation observed at inspection ports."),
]

E101_P2: list[Line] = [
    ("h2", "3. HYDROSTATIC / LEAK TEST"),
    ("t", "Tube side test pressure 21.0 barg held 30 min - no leak observed."),
    ("t", "Shell side test pressure 18.2 barg held 30 min - no leak observed."),
    ("gap", ""),
    ("h2", "4. ASSESSMENT"),
    ("t", "Minimum measured thickness is 9.2 mm at CML-3, against t-min of 8.4 mm."),
    ("t", "Remaining corrosion margin is 0.8 mm. Measured corrosion rate is 0.25 mm/year"),
    ("t", "(comparison with the 2022 survey). Estimated remaining life at CML-3: 3.2 years."),
    ("gap", ""),
    ("h2", "5. RECOMMENDATION"),
    ("t", "Fit for continued service until the next turnaround (2028), subject to:"),
    ("t", "(a) re-inspection of CML-3 within 12 months;"),
    ("t", "(b) replacement of 6 further tubes showing wall loss greater than 40%."),
    ("gap", ""),
    ("gap", ""),
    ("b", "Inspected by:  A. Deshmukh (Level II UT)"),
    ("gap", ""),
    ("gap", ""),
    ("gap", ""),
    ("b", "Reviewed by:  QA - Inspection Cell"),
]

V204: list[Line] = [
    ("h1", "PRESSURE VESSEL V-204 - UT THICKNESS SURVEY"),
    ("t", "Survey No: UT-2026-0311      Date: 03 Jul 2026      Surveyor: M. Iyer (Level II UT)"),
    ("t", "Vessel: V-204 LPG knock-out drum.  MAWP 16.0 barg.  Nominal shell thickness 14.0 mm."),
    ("t", "Minimum required thickness (t-min) 10.9 mm.  Corrosion allowance 3.0 mm."),
    ("gap", ""),
    ("h2", "READINGS"),
    ("m", "CML     LOCATION              2023 (mm)   2026 (mm)"),
    ("m", "CML-1   Top head                13.6        13.4"),
    ("m", "CML-2   Shell, north            13.1        12.6"),
    ("m", "CML-3   Shell, south            12.9        12.3"),
    ("m", "CML-4   Bottom head             12.2        11.6"),
    ("gap", ""),
    ("h2", "FINDINGS"),
    ("t", "Worst case CML-4: loss of 0.6 mm in 3 years, corrosion rate 0.2 mm/year."),
    ("t", "Remaining margin at CML-4 is 0.7 mm; remaining life 3.5 years."),
    ("t", "Internal inspection recommended at the 2027 shutdown. Instrument nozzle N5"),
    ("t", "shows sensor fouling consistent with the PT-4471 calibration drift."),
]

P310: list[Line] = [
    ("h1", "MAINTENANCE HISTORY - CENTRIFUGAL PUMP P-310"),
    ("t", "Service: crude charge pump.   Driver: 250 kW motor.   Location: crude unit"),
    ("gap", ""),
    ("m", "DATE         WORK ORDER   ACTIVITY"),
    ("m", "2025-02-11   WO-88213     Mechanical seal replaced (leak 12 drops/min)"),
    ("m", "2025-09-30   WO-90177     Bearing vibration 7.1 mm/s - bearings replaced"),
    ("m", "2026-03-14   WO-91842     Impeller wear ring clearance 0.9 mm - renewed"),
    ("m", "2026-08-02   WO-93320     Vibration 3.2 mm/s after alignment - acceptable"),
    ("gap", ""),
    ("t", "Mean time between failures over 24 months: 7.5 months."),
    ("t", "Next planned activity: seal flush plan upgrade at the 2028 turnaround."),
]

CS12: list[Line] = [
    ("h1", "CORROSION ALLOWANCE AND REMAINING LIFE - STANDARD CS-12"),
    ("t", "Scope: carbon-steel pressure equipment in hydrocarbon service."),
    ("gap", ""),
    ("h2", "1. REMAINING LIFE"),
    ("t", "Remaining life (years) = (t-actual - t-min) / corrosion rate (mm/year)."),
    ("t", "Use the thinnest reading at each condition monitoring location (CML)."),
    ("gap", ""),
    ("h2", "2. INSPECTION INTERVAL"),
    ("t", "Next inspection interval = the lesser of half the remaining life and 10 years."),
    ("gap", ""),
    ("h2", "3. ACCEPTANCE"),
    ("t", "Equipment with remaining life under 2 years requires an engineering assessment"),
    ("t", "and a documented approval before continued operation."),
]

PT4471_P1: list[Line] = [
    ("h1", "INSTRUMENT CALIBRATION RECORD"),
    ("t", "Tag: PT-4471   Type: pressure transmitter   Range 0-25 barg   Loop: V-204 overhead"),
    ("t", "Calibration date: 21 Aug 2026     Technician: K. Pillai"),
    ("gap", ""),
    ("m", "TEST POINT (barg)   AS FOUND (barg)   AS LEFT (barg)"),
    ("m", "0.0                 0.31              0.02"),
    ("m", "6.25                6.58              6.26"),
    ("m", "12.5                12.87             12.51"),
    ("m", "25.0                25.42             25.03"),
    ("gap", ""),
    ("t", "As-found error 1.7% of span exceeds the 0.5% tolerance: recorded as FAILED AS FOUND."),
    ("t", "Adjusted and passed as left. Impulse line flushed; fouling found at vessel nozzle."),
    ("gap", ""),
    ("gap", ""),
    ("b", "Calibrated by:  K. Pillai"),
]

INCIDENT: list[Line] = [
    ("h1", "INCIDENT REPORT - FLANGE LEAK AT E-101 INLET"),
    ("t", "Incident No: INC-2026-0092     Date: 29 Jul 2026     Severity: minor (no injury)"),
    ("gap", ""),
    ("h2", "WHAT HAPPENED"),
    ("t", "Hydrocarbon weep observed at the E-101 shell inlet flange during a routine round."),
    ("t", "Estimated release under 2 litres; area barricaded and the flange hot-bolted."),
    ("gap", ""),
    ("h2", "CAUSE"),
    ("t", "Gasket relaxation after a thermal cycle; bolt torque found 20% below specification."),
    ("gap", ""),
    ("h2", "ACTIONS"),
    ("t", "Replace the gasket at the turnaround; add E-101 inlet flange to the torque audit list."),
    ("t", "Link to inspection report IR-2026-0147 for shell condition."),
]


# -- the workbench scenario: lathe L-1 --------------------------------------------------------

_ALL = "process-engineering, instrumentation, quality-assurance"

POLICY1_REV1: list[Line] = [
    ("h1", "POLICY 1 - CAPITAL EQUIPMENT REPLACEMENT AND PURCHASE"),
    ("t", "Document: POL-01     Revision: 1     Effective: 01 Aug 2026     Owner: Plant Head"),
    ("t", "Applies to: production machine tools and workshop equipment at Unit 7."),
    ("gap", ""),
    ("h2", "1. REPLACEMENT CRITERIA"),
    ("t", "1.1 A machine qualifies for replacement when it is more than 20 years old."),
    ("t", "1.2 A machine also qualifies when its repair cost over 12 months exceeds 40% of replacement cost."),
    ("gap", ""),
    ("h2", "2. QUOTATIONS"),
    ("t", "2.1 One quotation is sufficient for purchases up to INR 5 lakh."),
    ("t", "2.2 Two quotations are required for purchases above INR 5 lakh."),
    ("gap", ""),
    ("h2", "3. APPROVAL"),
    ("t", "3.1 Purchases above INR 15 lakh require the approval of the Plant Head."),
    ("gap", ""),
    ("h2", "4. VENDORS"),
    ("t", "4.1 Vendors shall provide a warranty of at least one year."),
]

POLICY1_REV2: list[Line] = [
    ("h1", "POLICY 1 - CAPITAL EQUIPMENT REPLACEMENT AND PURCHASE"),
    ("t", "Document: POL-01     Revision: 2     Effective: 01 Sep 2026     Owner: Plant Head"),
    ("t", "Applies to: production machine tools and workshop equipment at Unit 7."),
    ("gap", ""),
    ("h2", "1. REPLACEMENT CRITERIA"),
    ("t", "1.1 A machine qualifies for replacement when it is more than 20 years old."),
    ("t", "1.2 A machine also qualifies when its repair cost over 12 months exceeds 30% of replacement cost."),
    ("t", "1.3 Replacement machines shall have motors of efficiency class IE3 or better."),
    ("gap", ""),
    ("h2", "2. QUOTATIONS"),
    ("t", "2.1 One quotation is sufficient for purchases up to INR 5 lakh."),
    ("t", "2.2 Two quotations are required for purchases above INR 5 lakh and up to INR 10 lakh."),
    ("t", "2.3 Three quotations are required for purchases above INR 10 lakh."),
    ("gap", ""),
    ("h2", "3. APPROVAL"),
    ("t", "3.1 Purchases above INR 10 lakh require the approval of the Plant Head."),
    ("gap", ""),
    ("h2", "4. VENDORS"),
    ("t", "4.1 Vendors shall provide a warranty of at least one year."),
    ("t", "4.2 Vendors shall have a service centre within 200 km of the plant."),
]

POLICY2: list[Line] = [
    ("h1", "POLICY 2 - MACHINE GUARDING AND WORKSHOP SAFETY"),
    ("t", "Document: POL-02     Revision: 3     Effective: 15 Jan 2026     Owner: HSE Manager"),
    ("gap", ""),
    ("h2", "1. LATHES"),
    ("t", "1.1 Every lathe chuck shall have a guard interlocked with the spindle drive."),
    ("t", "1.2 A lathe without an interlocked chuck guard shall not be operated after 31 Mar 2026."),
    ("t", "1.3 Emergency stop buttons shall be tested at the start of every shift."),
    ("gap", ""),
    ("h2", "2. GENERAL"),
    ("t", "2.1 Operators shall be trained and authorised for each machine they use."),
    ("t", "2.2 Gloves near rotating parts and unguarded chip removal are prohibited."),
]

POLICY3: list[Line] = [
    ("h1", "POLICY 3 - PREVENTIVE MAINTENANCE OF MACHINE TOOLS"),
    ("t", "Document: POL-03     Revision: 2     Effective: 01 Apr 2026     Owner: Maintenance Manager"),
    ("gap", ""),
    ("t", "1. Spindle runout shall be checked every quarter; the acceptance limit is 0.02 mm."),
    ("t", "2. Bed wear above 0.10 mm requires regrinding of the bed or replacement of the machine."),
    ("t", "3. Slideway lubrication shall be checked daily and logged."),
    ("t", "4. A machine with more than 4 breakdowns in 12 months shall be reviewed for replacement."),
]

LATHE_L1: list[Line] = [
    ("h1", "MACHINE SHOP SECTOR 1 - LATHE L-1 CONDITION REPORT"),
    ("t", "Report No: MS1-2026-031     Date: 18 Aug 2026     Prepared by: V. Patil (shop engineer)"),
    ("t", "Machine: L-1 conventional centre lathe, Sahyadri SMT-450, commissioned 2009."),
    ("t", "Swing over bed 450 mm, 1500 mm between centres, spindle motor 7.5 kW (efficiency class IE1)."),
    ("gap", ""),
    ("h2", "1. CONDITION"),
    ("t", "Spindle runout is 0.045 mm at the chuck face against an acceptance limit of 0.02 mm."),
    ("t", "Bed wear is 0.12 mm near the headstock; the tailstock is misaligned by 0.05 mm."),
    ("t", "The chuck guard is fitted but is not interlocked with the spindle drive."),
    ("t", "Headstock bearing noise was reported on 3 shifts in August."),
    ("gap", ""),
    ("h2", "2. RELIABILITY AND COST, LAST 12 MONTHS"),
    ("t", "Breakdowns: 6.  Downtime: 142 hours.  Repair cost: INR 3.9 lakh."),
    ("t", "Like-for-like replacement cost (conventional lathe of the same capacity): INR 12.5 lakh."),
    ("gap", ""),
    ("h2", "3. UTILISATION"),
    ("t", "Utilisation was 78% of available hours over two shifts; 41 jobs were waiting for L-1."),
    ("gap", ""),
    ("h2", "4. SHOP ENGINEER'S NOTE"),
    ("t", "Accuracy is no longer adequate for pump shaft sleeves, which need 0.02 mm."),
    ("t", "Recommend replacement with a CNC turning centre; requisition PR-2026-0418 raised."),
]

LATHE_LOG: list[Line] = [
    ("h1", "MACHINE SHOP SECTOR 2 - LATHE PRODUCTION LOG, AUGUST 2026"),
    ("t", "Prepared by: S. Kale (production supervisor)     Period: 01 to 31 Aug 2026"),
    ("gap", ""),
    ("m", "MACHINE  TYPE                BUILT  UTILISATION  SCRAP  FLANGE BLANK"),
    ("m", "L-2      CNC turning centre  2019   64%          1.2%   6.5 min"),
    ("m", "L-3      conventional lathe  2012   81%          4.8%   17 min"),
    ("gap", ""),
    ("t", "The scrap target for turned parts is 2.0%; L-3 exceeded it in every week of August."),
    ("t", "Turning backlog across the shop: 320 jobs, of which 118 are pump spares."),
    ("t", "Overtime on lathes in August: 96 hours. L-2 has about 30 hours a week of spare capacity."),
    ("t", "Sleeve jobs moved from L-1 to L-3 in August raised L-3 scrap on those jobs."),
]

REQUISITION: list[Line] = [
    ("h1", "PURCHASE REQUISITION PR-2026-0418"),
    ("t", "Date raised: 04 Sep 2026     Raised by: machine shop sector 1     Budget: CAPEX-26-MS"),
    ("t", "Item: one 2-axis CNC turning centre, to replace lathe L-1."),
    ("gap", ""),
    ("m", "VENDOR                MODEL   QUOTED PRICE    DELIVERY  SERVICE CENTRE"),
    ("m", "Deccan Machine Tools  DT-250  INR 18.6 lakh   10 weeks  Pune, 160 km"),
    ("gap", ""),
    ("t", "Quotations attached: 1 (single source)."),
    ("t", "Justification: repair cost over 12 months is 31% of replacement cost (Policy 1, clause 1.2)."),
    ("t", "Approval route: Plant Head (value above INR 10 lakh)."),
    ("t", "Status: awaiting procurement review."),
]

QUOTATIONS: list[Line] = [
    ("h1", "VENDOR QUOTATION COMPARISON - CNC TURNING CENTRES"),
    ("t", "Prepared by: procurement cell     Date: 11 Sep 2026     Reference: PR-2026-0418"),
    ("gap", ""),
    ("m", "VENDOR                MODEL   PRICE      DELIVERY  SERVICE CENTRE     MOTOR"),
    ("m", "Deccan Machine Tools  DT-250  18.6 lakh  10 weeks  Pune, 160 km       IE3"),
    ("m", "Konkan Tooling Co.    KT-2A   16.9 lakh  14 weeks  Ratnagiri, 60 km   IE3"),
    ("m", "Western Precision     WP-T20  21.4 lakh  8 weeks   Ahmedabad, 540 km  IE4"),
    ("gap", ""),
    ("t", "WP-T20 does not meet the 200 km service-centre requirement of Policy 1 Revision 2."),
    ("t", "KT-2A is the lowest compliant quotation, but its 400 mm swing is below L-1's 450 mm."),
    ("t", "Negotiated prices are commercially confidential."),
]

REF_DIGEST: list[Line] = [
    ("h1", "TECHNOLOGY DIGEST - CNC TURNING CENTRES AND CONVENTIONAL LATHES"),
    ("t", "Reference library item REF-01. Imported 02 Sep 2026 after review. Public literature summary."),
    ("gap", ""),
    ("h2", "1. PRODUCTIVITY"),
    ("t", "CNC turning centres typically cut cycle time by 50 to 65% on repeat batch work."),
    ("t", "Scrap on turned parts typically falls below 2% with CNC and in-process gauging."),
    ("gap", ""),
    ("h2", "2. RETROFIT OR REPLACE"),
    ("t", "A CNC retrofit of a conventional lathe costs 35 to 50% of a new turning centre."),
    ("t", "A retrofit does not correct bed wear above 0.10 mm; such machines should be replaced."),
    ("gap", ""),
    ("h2", "3. ENERGY AND PEOPLE"),
    ("t", "IE3 spindle motors use 8 to 12% less energy than IE1 motors at typical duty."),
    ("t", "Conventional machinists typically need 3 to 4 weeks of training on a CNC turning centre."),
]

REF_CATALOGUE: list[Line] = [
    ("h1", "VENDOR CATALOGUE EXTRACT - 2-AXIS CNC TURNING CENTRES"),
    ("t", "Reference library item REF-02. Imported 02 Sep 2026 after review. List prices only."),
    ("gap", ""),
    ("m", "MODEL   MAKER                  SWING   SPINDLE MOTOR  LIST PRICE"),
    ("m", "DT-250  Deccan Machine Tools   450 mm  11 kW IE3      INR 17 to 19 lakh"),
    ("m", "KT-2A   Konkan Tooling Co.     400 mm  7.5 kW IE3     INR 15 to 17 lakh"),
    ("m", "WP-T20  Western Precision      500 mm  15 kW IE4      INR 20 to 23 lakh"),
    ("gap", ""),
    ("t", "All three models accept ISO G-code programs and offer an optional bar feeder."),
    ("t", "Swing is the largest diameter a machine can turn over its bed."),
]

#: (file name, pages, marking) for every born-digital document in the lathe scenario.
WORKBENCH_DOCUMENTS: list[tuple[str, list[list[Line]], str]] = [
    ("POL-01_capital_equipment_policy_rev1.pdf", [POLICY1_REV1], "INTERNAL"),
    ("POL-01_capital_equipment_policy_rev2.pdf", [POLICY1_REV2], "INTERNAL"),
    ("POL-02_machine_guarding_policy.pdf", [POLICY2], "INTERNAL"),
    ("POL-03_preventive_maintenance_policy.pdf", [POLICY3], "INTERNAL"),
    ("MS1-2026-031_lathe_L-1_condition_report.pdf", [LATHE_L1], "INTERNAL"),
    ("MS2_lathe_production_log_2026-08.pdf", [LATHE_LOG], "INTERNAL"),
    ("PR-2026-0418_lathe_purchase_requisition.pdf", [REQUISITION], "INTERNAL"),
    ("PRC_cnc_turning_centre_quotations.pdf", [QUOTATIONS], "CONFIDENTIAL"),
    ("REF-01_cnc_turning_technology_digest.pdf", [REF_DIGEST], "PUBLIC"),
    ("REF-02_cnc_turning_centre_catalogue.pdf", [REF_CATALOGUE], "PUBLIC"),
]


def _check_widths(lines: Sequence[Line]) -> None:
    """Keep every line inside the page: a clipped line is a clipped fact."""
    for style, text in lines:
        limit = {"m": 84, "h1": 66, "h2": 80}.get(style, 100)
        if len(text) > limit:
            raise ValueError(f"line too long for the page ({len(text)} > {limit}): {text}")

def main(argv: Sequence[str] = ()) -> None:
    force = "--force" in argv
    OUT.mkdir(parents=True, exist_ok=True)
    written: list[str] = []

    def wanted(name: str) -> Path | None:
        path = OUT / name
        if path.exists() and not force:
            return None
        written.append(name)
        return path

    if (path := wanted("IR-2026-0147_E-101_inspection_report.pdf")) is not None:
        scanned(path, [E101_P1, E101_P2], "INTERNAL",
                stamp=["QA INSPECTED", "14 AUG 2026", "INSP. CELL"], stamp_at=(0.70, 0.47),
                signature_at=(0.47, 0.378), seed=11)
    if (path := wanted("UT-2026-0311_V-204_thickness_survey.pdf")) is not None:
        born_digital(path, [V204], "CONFIDENTIAL")
    if (path := wanted("P-310_maintenance_history.pdf")) is not None:
        born_digital(path, [P310], "INTERNAL")
    if (path := wanted("CS-12_corrosion_allowance_standard.pdf")) is not None:
        born_digital(path, [CS12], "PUBLIC")
    if (path := wanted("PT-4471_calibration_record.pdf")) is not None:
        scanned(path, [PT4471_P1], "CONFIDENTIAL",
                stamp=["CALIBRATED", "21 AUG 2026", "I&C LAB"], stamp_at=(0.74, 0.42),
                signature_at=(0.36, 0.348), seed=23)
    if (path := wanted("INC-2026-0092_E-101_flange_leak.pdf")) is not None:
        born_digital(path, [INCIDENT], "CONFIDENTIAL")
    if (path := wanted("V-204_nameplate_photo.png")) is not None:
        nameplate(path)
    for name, pages, marking in WORKBENCH_DOCUMENTS:
        for page in pages:
            _check_widths(page)
        if (path := wanted(name)) is not None:
            born_digital(path, pages, marking)
    print(f"corpus in {OUT}: wrote {len(written)} file(s)" + (f": {', '.join(written)}" if written else ""))


if __name__ == "__main__":
    import sys

    main(sys.argv[1:])
