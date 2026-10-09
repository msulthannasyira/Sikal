"""Pembuatan laporan PDF hasil analisis kesesuaian lahan padi.

Menggunakan ReportLab untuk menyusun dokumen laporan terstruktur dari satu
record AnalysisResult beserta hasil perhitungan SAW-nya.
"""
import io
from datetime import datetime

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_JUSTIFY
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import cm
from reportlab.platypus import (Paragraph, SimpleDocTemplate, Spacer, Table,
                                TableStyle)

from saw import CRITERIA, CRITERIA_LABELS

# Warna kelas selaras dengan tampilan web (detail.html)
KELAS_COLOR = {
    "S1": colors.HexColor("#7B9163"),
    "S2": colors.HexColor("#3288BD"),
    "S3": colors.HexColor("#FED45C"),
    "N":  colors.HexColor("#D53E4F"),
}

# Warna teks badge agar kontras (S3 kuning → teks gelap)
KELAS_TEXT_COLOR = {
    "S1": colors.white,
    "S2": colors.white,
    "S3": colors.HexColor("#212529"),
    "N":  colors.white,
}

PARAM_UNITS = {
    "slope": "%",
    "drainage": "TWI",
    "soil_depth": "cm",
    "soil_texture": "% liat",
    "soil_type": "kode USDA",
    "lulc": "kode ESA",
    "precipitation": "mm/tahun",
    "temperature": "°C",
    "distance_road": "meter",
    "distance_river": "meter",
}

SCORE_LABELS = {0: "Terbangun", 1: "Buruk", 2: "Cukup", 3: "Baik", 4: "Optimal"}


def _fmt_dt(value: str) -> str:
    """Format string datetime ISO menjadi 'YYYY-MM-DD HH:MM'."""
    if not value:
        return "-"
    return value[:16].replace("T", " ")


def build_analysis_pdf(row, saw: dict, raw: dict, kelas_info: dict = None) -> bytes:
    """Bangun laporan PDF dari satu hasil analisis.

    Args:
        row: sqlite3.Row hasil analisis (kolom name, description, username, dst.)
        saw: hasil calculate_saw()
        raw: dict parameter mentah
        kelas_info: data master kelas berkunci kode (dari tabel ``kelas``),
            sumber penjelasan potensi/karakteristik/rekomendasi.

    Returns:
        Konten PDF dalam bentuk bytes.
    """
    kelas_info = kelas_info or {}
    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=A4,
        leftMargin=2 * cm, rightMargin=2 * cm,
        topMargin=1.6 * cm, bottomMargin=1.6 * cm,
        title=f"Laporan Analisis - {row['name']}",
        author="SiPadi",
    )

    base = getSampleStyleSheet()
    styles = {
        "title": ParagraphStyle("rtitle", parent=base["Title"], fontSize=16,
                                 textColor=colors.HexColor("#2f4128"), spaceAfter=2),
        "subtitle": ParagraphStyle("rsubtitle", parent=base["Normal"], fontSize=10,
                                   alignment=TA_CENTER, textColor=colors.HexColor("#6c757d")),
        "h2": ParagraphStyle("rh2", parent=base["Heading2"], fontSize=12,
                             textColor=colors.HexColor("#2f4128"), spaceBefore=14, spaceAfter=6),
        "body": ParagraphStyle("rbody", parent=base["Normal"], fontSize=10, leading=14),
        "just": ParagraphStyle("rjust", parent=base["Normal"], fontSize=9.5, leading=13,
                               alignment=TA_JUSTIFY),
        "small": ParagraphStyle("rsmall", parent=base["Normal"], fontSize=8.5,
                               textColor=colors.HexColor("#6c757d")),
        "cell": ParagraphStyle("rcell", parent=base["Normal"], fontSize=8.5, leading=11),
        "cellb": ParagraphStyle("rcellb", parent=base["Normal"], fontSize=8.5, leading=11,
                               fontName="Helvetica-Bold"),
    }

    kelas = saw["kelas"]
    elems = []

    # ── Header ──────────────────────────────────────────────────────────
    elems.append(Paragraph("LAPORAN ANALISIS KESESUAIAN LAHAN PADI", styles["title"]))
    elems.append(Paragraph("Sistem Analisis Kesesuaian Lahan Padi (SiPadi) — Metode Simple Additive Weighting (SAW)",
                           styles["subtitle"]))
    elems.append(Spacer(1, 0.5 * cm))

    # ── Informasi Analisis ──────────────────────────────────────────────
    elems.append(Paragraph("1. Informasi Analisis", styles["h2"]))
    desc = row["description"] if row["description"] else "Tidak ada deskripsi"
    info_rows = [
        [Paragraph("Nama Analisis", styles["cellb"]), Paragraph(row["name"] or "-", styles["cell"])],
        [Paragraph("Deskripsi", styles["cellb"]), Paragraph(desc, styles["cell"])],
        [Paragraph("Dianalisis oleh", styles["cellb"]), Paragraph(row["username"] or "-", styles["cell"])],
        [Paragraph("Tanggal Analisis", styles["cellb"]), Paragraph(_fmt_dt(row["created_at"]), styles["cell"])],
    ]
    if row["updated_at"] and row["updated_at"] != row["created_at"]:
        info_rows.append([Paragraph("Terakhir Diperbarui", styles["cellb"]),
                          Paragraph(_fmt_dt(row["updated_at"]), styles["cell"])])
    info_tbl = Table(info_rows, colWidths=[4 * cm, 12.7 * cm])
    info_tbl.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("BACKGROUND", (0, 0), (0, -1), colors.HexColor("#f1f4ee")),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#dee2e6")),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ("LEFTPADDING", (0, 0), (-1, -1), 7),
        ("RIGHTPADDING", (0, 0), (-1, -1), 7),
    ]))
    elems.append(info_tbl)

    # ── Hasil Kesesuaian ────────────────────────────────────────────────
    elems.append(Paragraph("2. Hasil Kesesuaian", styles["h2"]))
    kelas_cell = ParagraphStyle("kcell", parent=styles["cell"], alignment=TA_CENTER,
                                fontName="Helvetica-Bold", fontSize=22,
                                textColor=KELAS_TEXT_COLOR.get(kelas, colors.white))
    label_style = ParagraphStyle("lstyle", parent=styles["cell"], fontSize=11,
                                 fontName="Helvetica-Bold")
    score_style = ParagraphStyle("sstyle", parent=styles["cell"], fontSize=18,
                                 fontName="Helvetica-Bold",
                                 textColor=colors.HexColor("#198754"))
    hasil_tbl = Table([
        [Paragraph(kelas, kelas_cell),
         [Paragraph("Kelas Kesesuaian", styles["small"]),
          Paragraph(saw["kelas_label"], label_style),
          Spacer(1, 0.2 * cm),
          Paragraph("Skor SAW Total", styles["small"]),
          Paragraph(f"{saw['total']:.4f}", score_style)]],
    ], colWidths=[4 * cm, 12.7 * cm])
    hasil_tbl.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("BACKGROUND", (0, 0), (0, 0), KELAS_COLOR.get(kelas, colors.grey)),
        ("BOX", (0, 0), (-1, -1), 0.5, colors.HexColor("#dee2e6")),
        ("TOPPADDING", (0, 0), (-1, -1), 10),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 10),
        ("LEFTPADDING", (1, 0), (1, 0), 12),
    ]))
    elems.append(hasil_tbl)

    # ── Penjelasan kelas (bersumber dari tabel kelas) ───────────────────
    ki = kelas_info.get(kelas)
    if ki and ki.get("potensi"):
        elems.append(Spacer(1, 0.3 * cm))
        elems.append(Paragraph(f"<b>Potensi Lahan.</b> {ki['potensi']}", styles["just"]))
        elems.append(Spacer(1, 0.15 * cm))
        elems.append(Paragraph(f"<b>Karakteristik &amp; Pembatas.</b> {ki['karakteristik']}", styles["just"]))
        elems.append(Spacer(1, 0.15 * cm))
        elems.append(Paragraph(f"<b>Rekomendasi.</b> {ki['rekomendasi']}", styles["just"]))

    # ── Tabel Nilai Parameter ───────────────────────────────────────────
    elems.append(Paragraph("3. Nilai Parameter (10 Kriteria)", styles["h2"]))
    header = ["Parameter", "Nilai Mentah", "Satuan", "Rating", "Status",
              "Normal.", "Bobot", "Terbobot"]
    data = [[Paragraph(h, styles["cellb"]) for h in header]]
    for key in CRITERIA:
        rv = raw.get(key)
        if rv is None:
            rv_str = "—"
        elif isinstance(rv, str):
            rv_str = rv
        else:
            rv_str = f"{rv:.4f}"
        sc = saw["scores"][key]
        data.append([
            Paragraph(CRITERIA_LABELS[key], styles["cell"]),
            Paragraph(rv_str, styles["cell"]),
            Paragraph(PARAM_UNITS.get(key, "—"), styles["cell"]),
            Paragraph(f"{sc}/4", styles["cell"]),
            Paragraph(SCORE_LABELS.get(sc, "-"), styles["cell"]),
            Paragraph(f"{saw['normalized'][key]}", styles["cell"]),
            Paragraph(f"{saw['weights'][key]}", styles["cell"]),
            Paragraph(f"{saw['weighted'][key]}", styles["cellb"]),
        ])
    # Baris total
    data.append([
        Paragraph("Total Skor SAW", styles["cellb"]), "", "", "", "", "", "",
        Paragraph(f"{saw['total']:.4f}", styles["cellb"]),
    ])

    param_tbl = Table(data, colWidths=[3.3 * cm, 2.2 * cm, 1.9 * cm, 1.4 * cm,
                                       1.7 * cm, 1.8 * cm, 1.6 * cm, 1.8 * cm])
    param_tbl.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#7B9163")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#dee2e6")),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("ALIGN", (1, 0), (-1, -1), "CENTER"),
        ("ALIGN", (0, 0), (0, -1), "LEFT"),
        ("ROWBACKGROUNDS", (0, 1), (-1, -2), [colors.white, colors.HexColor("#f8f9fa")]),
        ("BACKGROUND", (0, -1), (-1, -1), colors.HexColor("#eaf0e4")),
        ("SPAN", (0, -1), (-2, -1)),
        ("ALIGN", (0, -1), (-2, -1), "RIGHT"),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]))
    elems.append(param_tbl)
    elems.append(Spacer(1, 0.2 * cm))
    elems.append(Paragraph(
        "Skor dihitung menggunakan metode Simple Additive Weighting (SAW) dengan 10 kriteria berbobot sama. "
        "Klasifikasi: S1 ≥ 0,75 (Sangat Sesuai); S2 0,50–0,74 (Cukup Sesuai); "
        "S3 0,25–0,49 (Sesuai Marginal); N &lt; 0,25 (Tidak Sesuai).",
        styles["small"]))

    # ── Footer dokumen ──────────────────────────────────────────────────
    elems.append(Spacer(1, 0.6 * cm))
    elems.append(Paragraph(
        f"Laporan dibuat otomatis oleh SiPadi pada {datetime.now().strftime('%d-%m-%Y %H:%M')} WIB.",
        styles["small"]))

    doc.build(elems)
    pdf = buf.getvalue()
    buf.close()
    return pdf
