"""Simple Additive Weighting (SAW) untuk analisis kesesuaian lahan padi.

Setiap parameter dinilai dengan rating 1-4 lalu dinormalisasi ke 0-1.
Semua kriteria dianggap benefit (nilai lebih tinggi = lebih baik),
kecuali jarak_jalan dan jarak_sungai yang sudah di-encode agar skor
tinggi = dekat (benefit).

Bobot setiap kriteria dianggap sama (equal weight).
"""

# ── Rating functions (crisp → score 1-4) ───────────────────────────────

def rate_slope(pct):
    """Kelerengan (%)."""
    if pct is None:
        return 1
    if pct <= 3:
        return 4
    if pct <= 8:
        return 3
    if pct <= 30:
        return 2
    return 1


def rate_drainage(twi_value):
    """Drainase berdasarkan TWI proxy = -ln(tan(slope)), range ~0-7.
    TWI tinggi → lahan datar → air menggenang (baik untuk padi)
    TWI rendah → lahan curam → drainase cepat
        TWI > 5.5  → datar/agak terhambat (skor 4)
        3.5 - 5.5  → agak baik / baik      (skor 3)
        1.5 - 3.5  → drainase baik          (skor 2)
        < 1.5      → drainase cepat/curam   (skor 1)
    """
    if twi_value is None:
        return 1
    if twi_value > 5.5:
        return 4
    if twi_value > 3.5:
        return 3
    if twi_value > 1.5:
        return 2
    return 1


def rate_soil_depth(coded_value):
    """Kedalaman tanah efektif (cm), sesuai tabel kesesuaian:
        > 50 cm   → skor 4
        40 – 50   → skor 3
        25 – 40   → skor 2
        < 25      → skor 1
    """
    if coded_value is None:
        return 1
    if coded_value > 50:
        return 4
    if coded_value >= 40:
        return 3
    if coded_value >= 25:
        return 2
    return 1


def rate_soil_texture(clay_pct):
    """Tekstur tanah (proxy dari % clay SoilGrids), disesuaikan dengan tabel:
        Halus / agak halus  (clay ≥ 30 %)  → skor 4
        Sedang              (clay 18–30 %) → skor 3
        Agak kasar          (clay 10–18 %) → skor 2
        Kasar               (clay < 10 %)  → skor 1
    """
    if clay_pct is None:
        return 1
    if clay_pct >= 30:
        return 4
    if clay_pct >= 18:
        return 3
    if clay_pct >= 10:
        return 2
    return 1


# OpenLandMap SOL_GRTGROUP_USDA-SOILTAX_C/v01 uses USDA great-group codes 0-433.
# USDA great groups mapped to FAO equivalents:
#   Cambisols ≈ Inceptisols (USDA)  → codes ~160-200+ range
#   Fluvisols ≈ Entisols/Fluvents   → codes ~80-120 range
#   Acrisols  ≈ Ultisols             → codes ~380-433 range
# We use broad ranges covering the soil orders.
_INCEPTISOL_RANGE = range(160, 231)   # Cambisols equivalent
_ENTISOL_RANGE = range(80, 121)       # Fluvisols equivalent
_ULTISOL_RANGE = range(380, 434)      # Acrisols equivalent


def rate_soil_type(code):
    """Jenis tanah dari USDA great-group code (0-433), sesuai tabel:
    Cambisols (Inceptisols) → 4, Fluvisols (Entisols) → 3,
    Acrisols (Ultisols) → 2, lain-lain → 1.
    """
    if code is None:
        return 1
    code = int(code)
    if code in _INCEPTISOL_RANGE:
        return 4
    if code in _ENTISOL_RANGE:
        return 3
    if code in _ULTISOL_RANGE:
        return 2
    return 1


# ESA WorldCover class codes
def rate_lulc(lulc_code):
    """Penggunaan lahan berdasarkan ESA WorldCover code.
    10 = Tree cover         → skor 2 (perkebunan hutan)
    20 = Shrubland          → skor 3 (belukar)
    30 = Grassland          → skor 3 (tanah terbuka)
    40 = Cropland           → skor 4 (pertanian/sawah)
    50 = Built-up           → skor 0 (lahan terbangun, otomatis N)
    60 = Bare/sparse        → skor 3 (tanah terbuka)
    70 = Snow/ice
    80 = Water
    90 = Herbaceous wetland → skor 2 (belukar rawa)
    95 = Mangroves
    100 = Moss/lichen
    """
    if lulc_code is None:
        return 1
    lulc_code = int(lulc_code)
    mapping = {
        10: 2,   # hutan → perkebunan hutan
        20: 3,   # belukar
        30: 3,   # grassland ~ tanah terbuka
        40: 4,   # cropland / sawah
        50: 0,   # lahan terbangun → tidak sesuai (skor 0)
        60: 3,   # bare → tanah terbuka
        70: 1,
        80: 1,
        90: 2,   # wetland → belukar rawa
        95: 2,
        100: 1,
    }
    return mapping.get(lulc_code, 1)


def rate_precipitation(mm_year):
    """Curah hujan (mm/tahun)."""
    if mm_year is None:
        return 1
    if mm_year > 2000:
        return 4
    if mm_year >= 1000:
        return 3
    return 1


def rate_temperature(deg_c):
    """Temperatur rata-rata (°C)."""
    if deg_c is None:
        return 1
    if 24 <= deg_c <= 29:
        return 4
    if 22 <= deg_c < 24 or 29 < deg_c <= 32:
        return 3
    if 18 <= deg_c < 22 or 32 < deg_c <= 35:
        return 2
    return 1


def rate_distance_road(m):
    """Jarak dari jalan (meter)."""
    if m is None:
        return 1
    if m <= 1000:
        return 4
    if m <= 2000:
        return 3
    if m <= 4000:
        return 2
    return 1


def rate_distance_river(m):
    """Jarak dari sungai (meter)."""
    if m is None:
        return 1
    if m <= 500:
        return 4
    if m <= 1000:
        return 3
    if m <= 2000:
        return 2
    return 1


# ── SAW Calculation ────────────────────────────────────────────────────

CRITERIA = [
    "slope",
    "drainage",
    "soil_depth",
    "soil_texture",
    "soil_type",
    "lulc",
    "precipitation",
    "temperature",
    "distance_road",
    "distance_river",
]

CRITERIA_LABELS = {
    "slope": "Kelerengan",
    "drainage": "Drainase",
    "soil_depth": "Kedalaman Tanah",
    "soil_texture": "Tekstur Tanah",
    "soil_type": "Jenis Tanah",
    "lulc": "Penggunaan Lahan",
    "precipitation": "Curah Hujan",
    "temperature": "Temperatur",
    "distance_road": "Jarak dari Jalan",
    "distance_river": "Jarak dari Sungai",
}

RATERS = {
    "slope": rate_slope,
    "drainage": rate_drainage,
    "soil_depth": rate_soil_depth,
    "soil_texture": rate_soil_texture,
    "soil_type": rate_soil_type,
    "lulc": rate_lulc,
    "precipitation": rate_precipitation,
    "temperature": rate_temperature,
    "distance_road": rate_distance_road,
    "distance_river": rate_distance_river,
}

MAX_SCORE = 4  # Skor tertinggi pada rating


def calculate_saw(raw_params: dict) -> dict:
    """Jalankan metode SAW dengan bobot sama.

    Returns dict berisi:
      - raw: {param: raw_value}
      - scores: {param: rating 1-4}
      - normalized: {param: rating / max_rating}  (benefit normalization)
      - weights: {param: w}
      - weighted: {param: normalized * w}
      - total: float  (skor akhir 0-1)
      - kelas: str    (S1/S2/S3/N)
    """
    n = len(CRITERIA)
    w = 1.0 / n  # Equal weight

    scores = {}
    normalized = {}
    weighted = {}

    for c in CRITERIA:
        raw_val = raw_params.get(c)
        rating = RATERS[c](raw_val)
        scores[c] = rating
        normalized[c] = round(rating / MAX_SCORE, 4)
        weighted[c] = round(normalized[c] * w, 4)

    # Lahan terbangun (lulc skor 0) → hasil akhir otomatis 0 / Tidak Sesuai
    if scores.get("lulc") == 0:
        for c in CRITERIA:
            normalized[c] = 0.0
            weighted[c] = 0.0
        return {
            "raw": raw_params,
            "scores": scores,
            "normalized": normalized,
            "weights": {c: round(w, 4) for c in CRITERIA},
            "weighted": weighted,
            "total": 0.0,
            "kelas": "N",
            "kelas_label": "Tidak Sesuai",
        }

    total = round(sum(weighted.values()), 4)

    # Klasifikasi kesesuaian
    # Interval kelas dihitung dengan persamaan ki = (Xt - Xr) / k, dengan
    # Xt = 1.0 (semua skor 4), Xr = 0.25 (semua skor 1), dan k = 4 kelas:
    #   ki = (1.0 - 0.25) / 4 = 0.1875
    # Sehingga batas kelas: 0.4375 / 0.6250 / 0.8125.
    if total >= 0.8125:
        kelas = "S1"
        kelas_label = "Sangat Sesuai"
    elif total >= 0.6250:
        kelas = "S2"
        kelas_label = "Cukup Sesuai"
    elif total >= 0.4375:
        kelas = "S3"
        kelas_label = "Sesuai Marginal"
    else:
        kelas = "N"
        kelas_label = "Tidak Sesuai"

    return {
        "raw": raw_params,
        "scores": scores,
        "normalized": normalized,
        "weights": {c: round(w, 4) for c in CRITERIA},
        "weighted": weighted,
        "total": total,
        "kelas": kelas,
        "kelas_label": kelas_label,
    }
