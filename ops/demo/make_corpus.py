"""Generate the synthetic demonstration corpus in ops/demo/corpus/.

Run once; the output is committed so every machine seeds the same bytes:

    python ops/demo/make_corpus.py

Every document is invented, marked "SYNTHETIC DEMONSTRATION DATA" on every page, and
belongs to a fictional plant ("Konkan Process Industries, Unit 7"). The numbers are
internally consistent on purpose -- the E-101 report's remaining-life figure really is
(9.2 - 8.4) / 0.25 -- so grounding checks and the calculator have something true to
find. Scanned documents are rendered, rotated, blurred and noised, and carry a stamp and
a hand-drawn signature, so OCR and the vision model have real work to do.

The classifications and ACLs in manifest.yaml are chosen so the three seeded identities
see visibly different subsets of the same query (ADR-0001 §Q7): the incident report is
denied to one engineer by classification and to the other by ACL.

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
    canvas = Canvas(str(path), pagesize=A4)
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


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    scanned(OUT / "IR-2026-0147_E-101_inspection_report.pdf", [E101_P1, E101_P2], "INTERNAL",
            stamp=["QA INSPECTED", "14 AUG 2026", "INSP. CELL"], stamp_at=(0.70, 0.47),
            signature_at=(0.47, 0.378), seed=11)
    born_digital(OUT / "UT-2026-0311_V-204_thickness_survey.pdf", [V204], "CONFIDENTIAL")
    born_digital(OUT / "P-310_maintenance_history.pdf", [P310], "INTERNAL")
    born_digital(OUT / "CS-12_corrosion_allowance_standard.pdf", [CS12], "PUBLIC")
    scanned(OUT / "PT-4471_calibration_record.pdf", [PT4471_P1], "CONFIDENTIAL",
            stamp=["CALIBRATED", "21 AUG 2026", "I&C LAB"], stamp_at=(0.74, 0.42),
            signature_at=(0.36, 0.348), seed=23)
    born_digital(OUT / "INC-2026-0092_E-101_flange_leak.pdf", [INCIDENT], "CONFIDENTIAL")
    nameplate(OUT / "V-204_nameplate_photo.png")
    print(f"corpus written to {OUT}")


if __name__ == "__main__":
    main()
