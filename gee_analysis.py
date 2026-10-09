"""Google Earth Engine data fetching for rice-field suitability analysis.

Each public function returns a **mean value** for the given polygon at
~30 m resolution.  The polygon is supplied as a GeoJSON-style list of
coordinate pairs [[lng, lat], …].
"""

import json
import logging
import os
import time

import ee
from config import Config

log = logging.getLogger('sipadi')

# Tandai baris log yang harus muncul di tampilan UI (informasi/hasil), bukan
# detail proses kerja. Gunakan sebagai: log.info(..., extra=DISPLAY)
DISPLAY = {'display': True}

_initialised = False

# Folder proyek — dipakai untuk menyelesaikan path kunci yang relatif.
_BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def _init_gee():
    global _initialised
    if _initialised:
        return
    if not Config.GEE_PRIVATE_KEY_JSON:
        raise RuntimeError("GEE_PRIVATE_KEY_JSON belum diset di file .env")

    # Resolusi path file kunci (mendukung path relatif terhadap folder proyek).
    key_path = Config.GEE_PRIVATE_KEY_JSON
    if not os.path.isabs(key_path):
        key_path = os.path.join(_BASE_DIR, key_path)
    if not os.path.exists(key_path):
        raise RuntimeError(f"File kunci GEE tidak ditemukan: {key_path}")

    # Project & service account DIBACA LANGSUNG dari isi file kunci JSON.
    # Dengan begini, mengganti akun GEE cukup menimpa file kuncinya saja —
    # tidak perlu mengubah .env sama sekali.
    with open(key_path, encoding="utf-8") as f:
        key_data = json.load(f)
    service_account = key_data.get("client_email")
    project = key_data.get("project_id")
    if not service_account or not project:
        raise RuntimeError(
            "File kunci GEE tidak valid: field 'client_email'/'project_id' tidak ada."
        )

    log.info("[GEE] Menginisialisasi koneksi ke Google Earth Engine…")
    log.info(f"[GEE] Project = {project} | Service account = {service_account}")
    t0 = time.perf_counter()
    credentials = ee.ServiceAccountCredentials(service_account, key_path)
    ee.Initialize(credentials, project=project)
    _initialised = True
    log.info(f"[GEE] Koneksi GEE berhasil ({time.perf_counter()-t0:.1f}s)")


def _make_roi(coords):
    """Convert [[lng,lat], …] to an ee.Geometry.Polygon."""
    return ee.Geometry.Polygon([coords])


# ── 1. Curah Hujan (CHIRPS, mm/year) ──────────────────────────────────
def get_precipitation(coords, year=2024):
    _init_gee()
    roi = _make_roi(coords)
    col = (
        ee.ImageCollection("UCSB-CHG/CHIRPS/DAILY")
        .filterDate(f"{year}-01-01", f"{year}-12-31")
        .filterBounds(roi)
    )
    annual = col.select("precipitation").sum().rename("precip_annual")
    val = annual.reduceRegion(
        reducer=ee.Reducer.mean(), geometry=roi, scale=30, maxPixels=1e9
    ).get("precip_annual")
    return ee.Number(val).getInfo()


# ── 2. Temperatur (ERA5-Land monthly, °C) ─────────────────────────────
def get_temperature(coords, year=2024):
    _init_gee()
    roi = _make_roi(coords)
    col = (
        ee.ImageCollection("ECMWF/ERA5_LAND/MONTHLY_AGGR")
        .filterDate(f"{year}-01-01", f"{year}-12-31")
        .filterBounds(roi)
    )
    # temperature_2m is in Kelvin
    mean_k = col.select("temperature_2m").mean()
    mean_c = mean_k.subtract(273.15).rename("temp_c")
    val = mean_c.reduceRegion(
        reducer=ee.Reducer.mean(), geometry=roi, scale=30, maxPixels=1e9
    ).get("temp_c")
    return ee.Number(val).getInfo()


# ── 3. Tekstur Tanah (SoilGrids – clay %) ─────────────────────────────
def get_soil_texture(coords):
    _init_gee()
    roi = _make_roi(coords)
    clay = ee.Image("OpenLandMap/SOL/SOL_CLAY-WFRACTION_USDA-3A1A1A_M/v02").select(
        "b0"
    )  # 0-2 cm
    val = clay.reduceRegion(
        reducer=ee.Reducer.mean(), geometry=roi, scale=30, maxPixels=1e9
    ).get("b0")
    return ee.Number(val).getInfo()


# ── 4. Kedalaman Tanah (proxy dari bulk density di kedalaman 200 cm) ───
def get_soil_depth(coords):
    _init_gee()
    roi = _make_roi(coords)
    # Bulk density at 200 cm depth (band b200). Lower bulk density at
    # deep layers indicates deeper effective soil.  We invert: return
    # an estimated depth in cm derived from all 6 standard depths.
    bd = ee.Image("OpenLandMap/SOL/SOL_BULKDENS-FINEEARTH_USDA-4A1H_M/v02")
    # Bands at 0,10,30,60,100,200 cm.  Count how many layers have
    # reasonable bulk density (< 180 → 10×kg/m³ threshold) as proxy for
    # effective soil depth.
    depths = [0, 10, 30, 60, 100, 200]
    bands  = ["b0", "b10", "b30", "b60", "b100", "b200"]
    # Build an image where each pixel = deepest layer with BD < 180
    depth_img = ee.Image(0)
    for d, b in zip(depths, bands):
        has_soil = bd.select(b).lt(180)  # valid soil layer
        depth_img = depth_img.where(has_soil, d)
    val = depth_img.rename("eff_depth").reduceRegion(
        reducer=ee.Reducer.mean(), geometry=roi, scale=30, maxPixels=1e9
    ).get("eff_depth")
    return ee.Number(val).getInfo()


# ── 5. Drainase (Topographic Wetness Index dari SRTM) ─────────────────
def get_drainage(coords):
    _init_gee()
    roi = _make_roi(coords)
    dem = ee.Image("USGS/SRTMGL1_003")
    slope_rad = ee.Terrain.slope(dem).multiply(3.14159265 / 180)
    # TWI proxy = -ln(tan(slope)), range ~0-7
    # Flat area → high TWI (air menggenang, baik untuk padi)
    # Curam     → low TWI (drainase cepat)
    tan_slope = slope_rad.tan().max(0.001)  # avoid division by zero
    twi = tan_slope.log().multiply(-1).rename("twi")  # range ~0-7
    val = twi.reduceRegion(
        reducer=ee.Reducer.mean(), geometry=roi, scale=30, maxPixels=1e9
    ).get("twi")
    return ee.Number(val).getInfo()


# ── 6. Jenis Tanah (USDA soil taxonomy great groups) ──────────────────
def get_soil_type(coords):
    _init_gee()
    roi = _make_roi(coords)
    # OpenLandMap USDA soil great groups (coded integer)
    soil = ee.Image("OpenLandMap/SOL/SOL_GRTGROUP_USDA-SOILTAX_C/v01").select("grtgroup")
    val = soil.reduceRegion(
        reducer=ee.Reducer.mode(), geometry=roi, scale=30, maxPixels=1e9
    ).get("grtgroup")
    return ee.Number(val).getInfo()


# ── 7. Kelerengan (SRTM DEM → slope %) ────────────────────────────────
def get_slope(coords):
    _init_gee()
    roi = _make_roi(coords)
    dem = ee.Image("USGS/SRTMGL1_003")
    slope = ee.Terrain.slope(dem)  # degrees
    # degrees → %   tan(deg) * 100
    slope_pct = slope.multiply(3.14159265 / 180).tan().multiply(100).rename("slope_pct")
    val = slope_pct.reduceRegion(
        reducer=ee.Reducer.mean(), geometry=roi, scale=30, maxPixels=1e9
    ).get("slope_pct")
    return ee.Number(val).getInfo()


# ── 8. LULC (ESA WorldCover 10 m) ─────────────────────────────────────
def get_lulc(coords):
    """Penggunaan lahan dari ESA WorldCover (primary source)."""
    _init_gee()
    roi = _make_roi(coords)
    lulc = ee.Image("ESA/WorldCover/v200/2021").select("Map")
    val = lulc.reduceRegion(
        reducer=ee.Reducer.mode(), geometry=roi, scale=30, maxPixels=1e9
    ).get("Map")
    return ee.Number(val).getInfo()


def get_built_up_from_ghsl(coords):
    """Deteksi area terbangun menggunakan GHSL (Global Human Settlement Layer).
    
    Returns:
        - 1.0 jika area terdeteksi sebagai built-up/settlement (GHSL built_surface > 0)
        - 0.0 jika bukan area terbangun
    """
    _init_gee()
    roi = _make_roi(coords)
    ghsl = ee.Image("JRC/GHSL/P2023A/GHS_BUILT_S/2020").select("built_surface")
    # built_surface > 0 menunjukkan adanya struktur/settlement terbangun
    built_fraction = (
        ghsl.gt(0)
        .rename("is_built")
        .unmask(0)
        .toFloat()
        .reduceRegion(
            reducer=ee.Reducer.mean(), geometry=roi, scale=30, maxPixels=1e9
        )
        .get("is_built")
        .getInfo()
    ) or 0.0
    return built_fraction


def get_lulc_crossvalidated(coords):
    """Penggunaan lahan dengan cross-validation menggunakan GHSL.
    
    Logika:
    1. Ambil LULC dari ESA WorldCover (primary)
    2. Jika ESA WorldCover = 50 (built-up), return 50
    3. Jika ESA WorldCover != 50, cek dengan GHSL:
       - Jika GHSL mendeteksi built-up (fraction > 0.2 / 20%), return 50 (override)
       - Jika GHSL tidak terdeteksi, return ESA WorldCover value
    
    Returns:
        - LULC code: 50 jika area terbangun (ESA atau GHSL), atau ESA code lainnya
    """
    _init_gee()
    roi = _make_roi(coords)
    
    # Primary: ESA WorldCover
    lulc_img = ee.Image("ESA/WorldCover/v200/2021").select("Map")
    esa_code = lulc_img.reduceRegion(
        reducer=ee.Reducer.mode(), geometry=roi, scale=30, maxPixels=1e9
    ).get("Map").getInfo()
    
    esa_code = int(esa_code) if esa_code is not None else None
    
    # Jika ESA sudah mendeteksi built-up, return langsung
    if esa_code == 50:
        log.info(f"[GEE] LULC Cross-validation: ESA detected built-up (50)")
        return 50
    
    # Secondary: GHSL untuk validasi silang
    ghsl = ee.Image("JRC/GHSL/P2023A/GHS_BUILT_S/2020").select("built_surface")
    ghsl_built_fraction = (
        ghsl.gt(0)
        .rename("is_built")
        .unmask(0)
        .toFloat()
        .reduceRegion(
            reducer=ee.Reducer.mean(), geometry=roi, scale=30, maxPixels=1e9
        )
        .get("is_built")
        .getInfo()
    ) or 0.0
    
    # Jika GHSL mendeteksi built-up significant (> 20% area), treat sebagai built-up
    if ghsl_built_fraction > 0.2:
        log.info(
            f"[GEE] LULC Cross-validation: ESA={esa_code}, but GHSL detected "
            f"built-up ({ghsl_built_fraction*100:.1f}%), override to 50"
        )
        return 50
    
    # Tidak ada deteksi built-up, return ESA code
    log.info(
        f"[GEE] LULC Cross-validation: ESA={esa_code}, GHSL built-up fraction="
        f"{ghsl_built_fraction*100:.1f}%, no override"
    )
    return esa_code if esa_code is not None else 1


# ── 9. Jarak dari Jalan (OpenStreetMap via rasterized global roads) ─────
def get_distance_road(coords):
    _init_gee()
    roi = _make_roi(coords)
    # Use the Global Roads Inventory Dataset from JRC
    # Alternative: compute from OSM features via ee.FeatureCollection
    # We use nighttime lights (VIIRS) as a proxy for road proximity:
    # brighter areas correlate strongly with road infrastructure.
    # However, the most reliable approach is to rasterize roads from
    # a known FeatureCollection. We'll use TIGER for the concept but
    # apply a global approach using the DMSP-OLS or a simulated distance.

    # Approach: Use the Global Human Settlement Layer (GHSL) built-up
    # surface as proxy for infrastructure/road network proximity
    ghsl = ee.Image("JRC/GHSL/P2023A/GHS_BUILT_S/2020").select("built_surface")
    # Built-up > 0 indicates infrastructure presence
    infra_mask = ghsl.gt(0).selfMask()
    distance = infra_mask.fastDistanceTransform(2048).sqrt().multiply(30).rename("dist_road")
    val = distance.reduceRegion(
        reducer=ee.Reducer.mean(), geometry=roi, scale=30, maxPixels=1e9
    ).get("dist_road")
    result = ee.Number(val).getInfo()
    return result if result is not None else 0


# ── 10. Jarak dari Sungai (HydroSHEDS drainage direction → rivers) ─────
def get_distance_river(coords):
    _init_gee()
    roi = _make_roi(coords)
    # Use HydroSHEDS flow accumulation: high values = rivers/streams
    # threshold 50 mendeteksi sungai kecil/anak sungai sekalipun
    flow_acc = ee.Image("WWF/HydroSHEDS/15ACC")  # flow accumulation
    river_mask = flow_acc.gt(50).selfMask()
    distance = river_mask.fastDistanceTransform(2048).sqrt().multiply(30).rename("dist_river")
    val = distance.reduceRegion(
        reducer=ee.Reducer.mean(), geometry=roi, scale=30, maxPixels=1e9
    ).get("dist_river")
    result = ee.Number(val).getInfo()
    return result if result is not None else 0


# ── Land/water validator ─────────────────────────────────────────────
def validate_land_area(coords):
    """Check that the polygon is land, not ocean or inland water body.

    Returns (True, None) if valid; (False, error_message) otherwise.
    """
    _init_gee()
    roi = _make_roi(coords)
    
    log.info(f"[GEE] Data polygon dari user:")
    log.info(f"[GEE]   - Jumlah titik: {len(coords)}")
    log.info(f"[GEE]   - Koordinat: {coords}")
    
    # Dapatkan bounds untuk info
    try:
        bounds = roi.bounds().getInfo()
        log.info(f"[GEE]   - Bounds: {bounds}")
    except:
        log.debug("[GEE]   - Bounds: (tidak bisa diambil)")

    # Check 1: area tidak boleh masuk ke wilayah negara LAIN.
    #
    # Kenapa bukan "apakah titik ada DI DALAM poligon Indonesia"?
    # FAO GAUL beresolusi kasar sehingga pulau-pulau sangat kecil
    # (mis. Gili Trawangan/Meno/Air) TIDAK terwakili dalam poligon Indonesia,
    # jadi pengecekan "di dalam Indonesia" salah menolaknya. Sebaliknya kita
    # cek apakah AREA beririsan dengan wilayah negara lain — memakai batas ASLI
    # tanpa buffer, sehingga perbatasan darat (mis. Kalimantan–Malaysia) tetap
    # presisi, sementara pulau kecil Indonesia tetap lolos (tidak masuk negara
    # mana pun). Laut/perairan ditangani oleh Check 2 di bawah.
    #
    # filterBounds(roi) bekerja pada SELURUH geometri poligon dalam satu
    # panggilan server, jadi tidak peduli polygon punya 5 atau ratusan titik dan
    # area interior (bukan hanya titik sudut) ikut tervalidasi.
    log.info("[GEE] Memvalidasi apakah area menyentuh wilayah negara lain…")

    dataset_used = None
    try:
        log.info("[GEE] Dataset: FAO GAUL 2015 (Global Administrative Units Layers)")
        admin = ee.FeatureCollection('FAO/GAUL/2015/level0')
        foreign = admin.filter(ee.Filter.neq('ADM0_NAME', 'Indonesia'))
        dataset_used = "FAO GAUL 2015"

        foreign_names = (
            foreign.filterBounds(roi).aggregate_array('ADM0_NAME').distinct().getInfo()
        )
        log.info(f"[GEE] Validasi dengan {dataset_used}: negara lain yang beririsan: {foreign_names or 'tidak ada'}")

        if foreign_names:
            log.warning(f"[GEE] ✗ DITOLAK: Area menyentuh wilayah negara lain: {foreign_names}")
            return False, "Area berada di luar wilayah Indonesia. Silakan pilih area di dalam wilayah Indonesia."

        log.info(f"[GEE] ✓ VALID: Area tidak menyentuh negara lain ({dataset_used})")

    except Exception as e:
        log.warning(f"[GEE] FAO GAUL error: {str(e)[:80]}")
        log.info("[GEE] Fallback ke USDOS LSIB 2017 (US State Department boundaries)")

        # Fallback ke USDOS jika FAO error
        try:
            countries = ee.FeatureCollection('USDOS/LSIB_SIMPLE/2017')
            foreign = countries.filter(ee.Filter.neq('country_na', 'Indonesia'))
            dataset_used = "USDOS LSIB 2017"

            foreign_names = (
                foreign.filterBounds(roi).aggregate_array('country_na').distinct().getInfo()
            )
            log.info(f"[GEE] Validasi dengan {dataset_used}: negara lain yang beririsan: {foreign_names or 'tidak ada'}")

            if foreign_names:
                log.warning(f"[GEE] ✗ DITOLAK: Area menyentuh wilayah negara lain: {foreign_names}")
                return False, "Area berada di luar wilayah Indonesia. Silakan pilih area di dalam wilayah Indonesia."

            log.info(f"[GEE] ✓ VALID: Area tidak menyentuh negara lain ({dataset_used})")

        except Exception as e2:
            log.error(f"[GEE] Fallback USDOS juga gagal: {str(e2)[:80]}")
            return False, "Sistem tidak dapat memverifikasi wilayah Indonesia. Coba lagi nanti."

    # Check 2: detect water using ESA WorldCover only
    # ESA WorldCover (class 80) = perairan besar (laut, danau)
    lulc = ee.Image("ESA/WorldCover/v200/2021").select("Map")
    gsw  = ee.Image("JRC/GSW1_4/GlobalSurfaceWater").select("max_extent")
    
    # Deteksi air dari ESA WorldCover (class 80) - HANYA INI YANG DIHITUNG
    esa_water_mask = lulc.eq(80).rename("esa_water").toFloat()
    esa_water_fraction = (
        esa_water_mask.reduceRegion(
            reducer=ee.Reducer.mean(), geometry=roi, scale=30, maxPixels=1e9
        )
        .get("esa_water")
        .getInfo()
    ) or 0.0
    
    # Deteksi air dari JRC Global Surface Water (untuk info saja, tidak dihitung)
    jrc_water_mask = gsw.eq(1).rename("jrc_water").toFloat()
    jrc_water_fraction = (
        jrc_water_mask.reduceRegion(
            reducer=ee.Reducer.mean(), geometry=roi, scale=30, maxPixels=1e9
        )
        .get("jrc_water")
        .getInfo()
    ) or 0.0
    
    # Deteksi sungai dari HydroSHEDS flow accumulation (untuk info saja, tidak dihitung)
    flow_acc = ee.Image("WWF/HydroSHEDS/15ACC")
    river_mask = flow_acc.gt(50).rename("river").toFloat()
    river_fraction = (
        river_mask.reduceRegion(
            reducer=ee.Reducer.mean(), geometry=roi, scale=30, maxPixels=1e9
        )
        .get("river")
        .getInfo()
    ) or 0.0
    
    # Water fraction HANYA berdasarkan ESA
    water_fraction = esa_water_fraction
    
    log.info(f"[GEE] Deteksi perairan: ESA={esa_water_fraction*100:.1f}% | (HydroSHEDS={river_fraction*100:.1f}% - info) | (JRC={jrc_water_fraction*100:.1f}% - info)")
    
    if water_fraction > 0.5:
        return False, "Area berada di perairan (laut/danau/sungai). Silakan pilih area daratan untuk dianalisis."

    return True, None


# ── Sumber data tiap parameter (nama dataset GEE + tahun rujukan) ───────
# Untuk precipitation & temperature, tahun mengikuti argumen `year` (None di sini).
PARAM_SOURCES = {
    "slope":          ("SRTM DEM (USGS/SRTMGL1_003)", "2000"),
    "drainage":       ("SRTM DEM (USGS/SRTMGL1_003)", "2000"),
    "soil_depth":     ("OpenLandMap SoilGrids (Bulk Density v02)", "2018"),
    "soil_texture":   ("OpenLandMap SoilGrids (Clay Fraction v02)", "2018"),
    "soil_type":      ("OpenLandMap SoilGrids (USDA Great Groups v01)", "2018"),
    "lulc":           ("ESA WorldCover v200 + GHSL P2023A (JRC)", "2021"),
    "precipitation":  ("CHIRPS Daily (UCSB-CHG)", None),
    "temperature":    ("ERA5-Land Monthly (ECMWF)", None),
    "distance_road":  ("GHSL Built-up P2023A (JRC)", "2020"),
    "distance_river": ("HydroSHEDS Flow Accumulation (WWF)", "2000"),
}


# ── Wrapper: fetch all parameters ──────────────────────────────────────
def fetch_all_parameters(coords, year=2024):
    """Return dict of all 10 raw parameter values dengan cross-validation untuk LULC."""
    steps = [
        ("slope",           lambda: get_slope(coords)),
        ("drainage",        lambda: get_drainage(coords)),
        ("soil_depth",      lambda: get_soil_depth(coords)),
        ("soil_texture",    lambda: get_soil_texture(coords)),
        ("soil_type",       lambda: get_soil_type(coords)),
        ("lulc",            lambda: get_lulc_crossvalidated(coords)),  # ← Cross-validated
        ("precipitation",   lambda: get_precipitation(coords, year)),
        ("temperature",     lambda: get_temperature(coords, year)),
        ("distance_road",   lambda: get_distance_road(coords)),
        ("distance_river",  lambda: get_distance_river(coords)),
    ]
    results = {}
    for i, (name, fn) in enumerate(steps, 1):
        src, src_year = PARAM_SOURCES.get(name, ("-", None))
        # precipitation/temperature memakai tahun dinamis dari argumen `year`
        src_year = src_year if src_year is not None else str(year)
        log.info(f"[GEE] ({i:>2}/{len(steps)}) Mengambil parameter: {name}…")
        t0 = time.perf_counter()
        val = fn()
        log.info(f"[GEE]       {name:<20} = {val}  ({time.perf_counter()-t0:.1f}s)", extra=DISPLAY)
        log.info(f"[GEE]       sumber: {src} [{src_year}]", extra=DISPLAY)
        results[name] = val
    return results
