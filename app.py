"""Sistem Analisis Kesesuaian Lahan Padi – Flask multi-page application."""
import json
import logging
import re
import secrets
import socket
import threading
import time
from datetime import datetime
from functools import wraps

from flask import (Flask, render_template, request, redirect,
                   url_for, flash, jsonify, abort, make_response, session)
from flask_login import login_required, login_user, logout_user, current_user
from werkzeug.security import generate_password_hash, check_password_hash
from itsdangerous import URLSafeTimedSerializer, BadData
import requests as _http

from config import Config
from database import init_db, get_db, get_kelas_map
from auth import login_manager, User
from gee_analysis import fetch_all_parameters, validate_land_area
from saw import calculate_saw, CRITERIA_LABELS, CRITERIA
from pdf_report import build_analysis_pdf


# ── GEE error helper ─────────────────────────────────────────────────
def _gee_error_msg(exc: Exception) -> str:
    """Kembalikan pesan error GEE yang ramah pengguna.
    Bedakan antara error jaringan/koneksi dengan error lainnya.
    """
    msg = str(exc)
    is_network = (
        isinstance(exc, (socket.gaierror, socket.timeout, ConnectionError, OSError))
        or any(kw in msg for kw in (
            "NameResolutionError", "getaddrinfo failed", "Failed to resolve",
            "Max retries exceeded", "ConnectionRefused", "timed out",
            "Network is unreachable",
        ))
    )
    if is_network:
        return (
            "Tidak dapat terhubung ke Google Earth Engine. "
            "Periksa koneksi internet server dan coba lagi."
        )
    return f"Terjadi kesalahan saat memproses data GEE: {exc}"


# ── Logging setup ────────────────────────────────────────────────────
class _ColorFormatter(logging.Formatter):
    COLORS = {
        logging.DEBUG:    '\033[36m',   # cyan
        logging.INFO:     '\033[32m',   # green
        logging.WARNING:  '\033[33m',   # yellow
        logging.ERROR:    '\033[31m',   # red
        logging.CRITICAL: '\033[35m',   # magenta
    }
    RESET = '\033[0m'
    BOLD  = '\033[1m'

    def format(self, record):
        color = self.COLORS.get(record.levelno, '')
        record.levelname = f"{color}{self.BOLD}{record.levelname:<8}{self.RESET}"
        record.msg       = f"{color}{record.msg}{self.RESET}"
        return super().format(record)

def _setup_logger():
    logger = logging.getLogger('sipadi')
    if logger.handlers:
        return logger
    logger.setLevel(logging.DEBUG)
    handler = logging.StreamHandler()
    handler.setFormatter(_ColorFormatter(
        fmt='%(asctime)s  %(levelname)s  %(message)s',
        datefmt='%H:%M:%S',
    ))
    logger.addHandler(handler)
    logger.propagate = False
    return logger

log = _setup_logger()

# Tandai baris log yang harus muncul di tampilan UI (informasi/hasil), bukan
# detail proses kerja. Gunakan sebagai: log.info(..., extra=DISPLAY)
DISPLAY = {'display': True}

_BULAN_ID = ['', 'Januari', 'Februari', 'Maret', 'April', 'Mei', 'Juni',
             'Juli', 'Agustus', 'September', 'Oktober', 'November', 'Desember']


def _tanggal_id(dt=None) -> str:
    """Tanggal Bahasa Indonesia, mis. '05 Juni 2026'."""
    dt = dt or datetime.now()
    return f"{dt.day:02d} {_BULAN_ID[dt.month]} {dt.year}"


# ── Per-request log capture ──────────────────────────────────────────────────
# Pola kode warna ANSI (mis. "\033[32m", "\033[0m") yang disisipkan formatter
# konsol — harus dibuang sebelum disimpan supaya tampilan log di UI bersih.
_ANSI_RE = re.compile(r'\x1b\[[0-9;]*m')

# Durasi proses, mis. "  (0.4s)" — berguna di konsol tapi tidak perlu di UI.
_ELAPSED_RE = re.compile(r'\s*\(\d+(?:\.\d+)?s\)')

# ID internal record, mis. " ID=11" — tidak perlu ditampilkan ke pengguna.
_ID_RE = re.compile(r'\s*ID=\d+')

# Baris nilai parameter GEE, mis. "[GEE]       slope                = 2.96".
# Nama teknis (slope, soil_depth, …) diganti label Bahasa Indonesia untuk UI,
# dan nilainya diberi satuan.
_GEE_PARAM_RE = re.compile(r'(\[GEE\]\s+)(\w+)\s+=\s+(.+)$')

# Satuan tiap parameter — hanya untuk tampilan UI.
PARAM_UNITS = {
    'slope': '%',
    'drainage': 'TWI',
    'soil_depth': 'cm',
    'soil_texture': '%',
    'soil_type': 'kode',
    'lulc': 'kode',
    'precipitation': 'mm/tahun',
    'temperature': '°C',
    'distance_road': 'm',
    'distance_river': 'm',
}


def _clean_for_display(msg: str) -> str:
    """Rapikan pesan log untuk tampilan UI (konsol PowerShell tetap apa adanya):
    buang kode warna ANSI, durasi proses, dan ID internal; terjemahkan nama
    parameter teknis ke label Bahasa Indonesia dan beri satuan pada nilainya."""
    msg = _ANSI_RE.sub('', msg)
    msg = _ELAPSED_RE.sub('', msg)
    msg = _ID_RE.sub('', msg)

    def _label(m):
        key, value = m.group(2), m.group(3)
        label = CRITERIA_LABELS.get(key)
        if not label:
            return m.group(0)
        unit = PARAM_UNITS.get(key, '')
        return f"{m.group(1)}{label:<20} = {value}{' ' + unit if unit else ''}"

    return _GEE_PARAM_RE.sub(_label, msg)


class _CaptureHandler(logging.Handler):
    """Captures log records into a list during an analysis run.

    Hanya mencatat baris yang ditandai ``extra={**DISPLAY}`` (informasi/hasil),
    sehingga tampilan log di UI ringkas — detail proses (koneksi, validasi,
    langkah pengambilan, dsb.) tetap tampil di konsol PowerShell saja.
    """
    def __init__(self, records: list):
        super().__init__()
        self._records = records

    def emit(self, record):
        if not getattr(record, 'display', False):
            return
        self._records.append({
            'time':    datetime.fromtimestamp(record.created).strftime('%H:%M:%S'),
            'level':   logging.getLevelName(record.levelno),
            'message': _clean_for_display(record.getMessage()),
        })


def _save_logs(result_id: int, run_number: int, records: list):
    if not records:
        return
    db = get_db()
    db.executemany(
        "INSERT INTO analysis_logs (result_id, run_number, logged_at, level, message)"
        " VALUES (?,?,?,?,?)",
        [(result_id, run_number, r['time'], r['level'], r['message']) for r in records],
    )
    db.commit()
    db.close()


# ── Inti analisis (dipakai bersama: analisis admin & persetujuan permintaan) ──
class AnalysisError(Exception):
    """Kesalahan analisis dengan pesan ramah pengguna + kode status HTTP.

    status 400 → input/area ditolak (validasi); 502 → gangguan GEE.
    """
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.message = message
        self.status = status


def _perform_analysis(name, description, coords, admin_id, requested_by=None):
    """Jalankan pipeline analisis (validasi → GEE → SAW → simpan) lalu kembalikan
    ``(result_id, result)``. Melempar :class:`AnalysisError` bila gagal.

    ``admin_id``     : pemilik (tenant) hasil analisis yang disimpan.
    ``requested_by`` : username pengaju (untuk permintaan user), hanya untuk log.
    """
    name = (name or "").strip()
    description = (description or "").strip()
    if not name:
        raise AnalysisError("Nama analisis wajib diisi.")
    if len(name) > 255:
        raise AnalysisError("Nama analisis maksimal 255 karakter.")
    if len(description) > 1000:
        raise AnalysisError("Deskripsi maksimal 1000 karakter.")
    if not coords or len(coords) < 3:
        raise AnalysisError("Polygon minimal 3 titik.")

    t_start = time.perf_counter()
    _captured: list = []
    _handler = _CaptureHandler(_captured)
    logging.getLogger('sipadi').addHandler(_handler)
    new_id = None
    try:
        log.info("──────────────────────────────────────────────────")
        log.info(f"[ANALISIS] Tanggal : {_tanggal_id()}", extra=DISPLAY)
        if requested_by:
            log.info(f"[ANALISIS] Permintaan dari user '{requested_by}'", extra=DISPLAY)
        log.info(f"[ANALISIS] Nama    : {name!r}", extra=DISPLAY)
        log.info(f"[ANALISIS] Deskripsi : {description!r}" if description else "[ANALISIS] Deskripsi : (kosong)", extra=DISPLAY)
        log.info(f"[ANALISIS] Titik   : {len(coords)} koordinat", extra=DISPLAY)

        log.info("[GEE] Memvalidasi area (cek Indonesia & perairan)…")
        t0 = time.perf_counter()
        try:
            valid, reason = validate_land_area(coords)
            log.info(f"[GEE] Validasi selesai ({time.perf_counter()-t0:.1f}s) → {'VALID' if valid else 'DITOLAK'}")
        except Exception as exc:
            log.error(f"[GEE] Error saat validasi: {exc}")
            raise AnalysisError(_gee_error_msg(exc), 502)
        if not valid:
            log.warning(f"[GEE] Area ditolak: {reason}")
            raise AnalysisError(reason, 400)

        log.info("[GEE] Mengambil 10 parameter dari Google Earth Engine…")
        t0 = time.perf_counter()
        try:
            raw = fetch_all_parameters(coords)
            elapsed = time.perf_counter() - t0
            log.info(f"[GEE] Semua parameter berhasil diambil ({elapsed:.1f}s)")
            for k, v in raw.items():
                log.debug(f"[GEE]   {k:<20} = {v}")
        except Exception as exc:
            log.error(f"[GEE] Error saat mengambil parameter: {exc}")
            raise AnalysisError(_gee_error_msg(exc), 502)

        log.info("[SAW] Menghitung SAW…")
        result = calculate_saw(raw)
        log.info(f"[SAW] Kelas: {result['kelas']}  |  Skor: {result['total']:.4f}", extra=DISPLAY)

        log.info("[DB] Menyimpan hasil ke database…")
        db = get_db()
        cur = db.execute(
            """INSERT INTO analysis_results
               (name, description, polygon_geojson, raw_params, saw_result,
                kelas_id, total_score, admin_id)
               VALUES (?,?,?,?,?,(SELECT id FROM kelas WHERE kode=?),?,?)""",
            (
                name,
                description,
                json.dumps(coords),
                json.dumps(raw),
                json.dumps(result),
                result["kelas"],
                result["total"],
                admin_id,
            ),
        )
        db.commit()
        new_id = cur.lastrowid
        db.close()
        total_elapsed = time.perf_counter() - t_start
        log.info(f"[DB] Disimpan dengan ID={new_id}")
        log.info(f"[ANALISIS] Selesai dalam {total_elapsed:.1f}s → ID={new_id}, Kelas={result['kelas']}")
        log.info("─" * 46)
    finally:
        logging.getLogger('sipadi').removeHandler(_handler)

    _save_logs(new_id, 1, _captured)
    return new_id, result


# ── Login attempt tracking (in-memory, thread-safe) ─────────────────────────
_login_attempts: dict = {}          # {ip: {'count': int, 'locked_until': float}}
_login_attempts_lock = threading.Lock()
LOGIN_MAX_ATTEMPTS  = 5
LOGIN_LOCKOUT_SECS  = 15 * 60       # 15 menit


def _client_ip() -> str:
    """Kembalikan IP klien, mempertimbangkan reverse-proxy X-Forwarded-For."""
    xff = request.headers.get("X-Forwarded-For", "")
    return xff.split(",")[0].strip() if xff else (request.remote_addr or "unknown")


def _check_lockout(ip: str) -> tuple:
    """Kembalikan (is_locked: bool, seconds_remaining: int)."""
    with _login_attempts_lock:
        data = _login_attempts.get(ip)
        if not data:
            return False, 0
        now = time.time()
        if data.get("locked_until", 0) > now:
            return True, int(data["locked_until"] - now)
        # Kunci telah berakhir – hapus entri lama
        if data.get("locked_until", 0) <= now and data.get("locked_until", 0) > 0:
            del _login_attempts[ip]
        return False, 0


def _record_failure(ip: str) -> int:
    """Catat satu kegagalan login. Kembalikan jumlah kegagalan saat ini."""
    with _login_attempts_lock:
        now  = time.time()
        data = _login_attempts.setdefault(ip, {"count": 0, "locked_until": 0.0})
        data["count"] += 1
        if data["count"] >= LOGIN_MAX_ATTEMPTS:
            data["locked_until"] = now + LOGIN_LOCKOUT_SECS
        return data["count"]


def _clear_attempts(ip: str) -> None:
    with _login_attempts_lock:
        _login_attempts.pop(ip, None)


def create_app():
    secret_key = Config.SECRET_KEY
    if not secret_key or len(secret_key) < 32:
        raise RuntimeError(
            "FLASK_SECRET_KEY wajib diatur dengan nilai acak minimal 32 karakter."
        )

    app = Flask(__name__)
    app.config.from_object(Config)

    login_manager.init_app(app)

    with app.app_context():
        init_db()

    log.info("\033[1m[SIPADI] Aplikasi dimulai – Sistem Analisis Kesesuaian Lahan Padi\033[0m")

    # ─────────────────────────── session timeout ──────────────────────────
    IDLE_TIMEOUT = 900  # 15 menit dalam detik

    @app.before_request
    def check_session_timeout():
        if current_user.is_authenticated:
            last_active = session.get('last_active')
            now = datetime.utcnow().timestamp()
            if last_active and (now - last_active) > IDLE_TIMEOUT:
                logout_user()
                session.clear()
                flash("Sesi Anda telah berakhir karena tidak ada aktivitas selama 15 menit. Silakan login kembali.", "warning")
                return redirect(url_for('login'))
            session['last_active'] = now

    # ─────────────────────────── template helpers ─────────────────────────
    @app.template_filter("kelas_badge")
    def kelas_badge_filter(kelas):
        return {"S1": "bg-success", "S2": "bg-primary",
                "S3": "bg-warning text-dark", "N": "bg-danger"}.get(kelas, "bg-secondary")

    @app.template_filter("kelas_label")
    def kelas_label_filter(kelas):
        return {"S1": "Sangat Sesuai", "S2": "Cukup Sesuai",
                "S3": "Sesuai Marginal", "N": "Tidak Sesuai"}.get(kelas, kelas)

    @app.context_processor
    def inject_pending_requests():
        """Jumlah permintaan yang masih menunggu → badge menu sidebar (admin)."""
        count = 0
        if current_user.is_authenticated and current_user.is_admin:
            db = get_db()
            count = db.execute(
                "SELECT COUNT(*) FROM analysis_requests"
                " WHERE admin_id=? AND status='pending'",
                (current_user.tenant_id,),
            ).fetchone()[0]
            db.close()
        return {"pending_requests_count": count}

    def admin_required(f):
        @wraps(f)
        def decorated(*args, **kwargs):
            if not current_user.is_authenticated or not current_user.is_admin:
                return redirect(url_for('dashboard'))
            return f(*args, **kwargs)
        return decorated

    def joined_required(f):
        """Redirect users who haven't joined any admin's tenant yet."""
        @wraps(f)
        def decorated(*args, **kwargs):
            if current_user.is_authenticated and current_user.needs_invite:
                return redirect(url_for("join"))
            return f(*args, **kwargs)
        return decorated

    def _verify_captcha(token: str) -> bool:
        """Verifikasi token hCaptcha ke server hCaptcha. Kembalikan True jika valid."""
        if not app.config.get("HCAPTCHA_ENABLED", True):
            return True  # Dinonaktifkan via HCAPTCHA_ENABLED=false
        secret   = app.config.get("HCAPTCHA_SECRET_KEY", "")
        site_key = app.config.get("HCAPTCHA_SITE_KEY", "")
        if not secret:
            return True  # Lewati jika tidak dikonfigurasi
        try:
            data = {
                "secret":   secret,
                "response": token or "",
                "remoteip": _client_ip(),
                "sitekey":  site_key,
            }
            resp = _http.post(
                "https://api.hcaptcha.com/siteverify",
                data=data,
                timeout=5,
            )
            result = resp.json()
            if not result.get("success"):
                log.warning(f"[CAPTCHA] Verifikasi gagal: {result.get('error-codes', [])}")
                return False
            return True
        except Exception as exc:
            log.warning(f"[CAPTCHA] Gagal memverifikasi CAPTCHA: {exc}")
            return False

    # ── "Trusted device" untuk melewati CAPTCHA ───────────────────────────
    # Cookie DITANDATANGANI dengan SECRET_KEY → tidak bisa dipalsukan manual
    # (mengetik trusted_device=1 sendiri akan gagal verifikasi tanda tangan).
    TRUSTED_MAX_AGE = 86400  # 24 jam

    def _set_trusted_cookie(resp):
        token = URLSafeTimedSerializer(app.config["SECRET_KEY"], salt="trusted-device").dumps("ok")
        resp.set_cookie("trusted_device", token, max_age=TRUSTED_MAX_AGE,
                        httponly=True, samesite="Lax",
                        secure=request.is_secure)  # hanya via HTTPS (otomatis aktif di produksi)

    def _is_trusted_device() -> bool:
        token = request.cookies.get("trusted_device", "")
        if not token:
            return False
        try:
            URLSafeTimedSerializer(app.config["SECRET_KEY"], salt="trusted-device").loads(
                token, max_age=TRUSTED_MAX_AGE)
            return True
        except BadData:
            return False  # tanda tangan tidak valid / kedaluwarsa → minta CAPTCHA lagi

    # ────────────────────────────── auth pages ────────────────────────────
    @app.route("/login", methods=["GET", "POST"])
    def login():
        if current_user.is_authenticated:
            return redirect(url_for("dashboard"))

        site_key        = app.config.get("HCAPTCHA_SITE_KEY", "")
        captcha_enabled = app.config.get("HCAPTCHA_ENABLED", True)
        ip              = _client_ip()
        locked, remaining = _check_lockout(ip)

        if locked:
            menit = remaining // 60
            detik = remaining % 60
            flash(
                f"Terlalu banyak percobaan login gagal. "
                f"Coba lagi dalam {menit} menit {detik} detik.",
                "danger",
            )
            return render_template("login.html", site_key=site_key, locked=True, skip_captcha=False, captcha_enabled=captcha_enabled)

        trusted = _is_trusted_device()

        if request.method == "POST":
            username = request.form.get("username", "").strip()
            password = request.form.get("password", "")

            # ── Verifikasi CAPTCHA (lewati jika device sudah trusted) ───────
            if not trusted:
                captcha_token = request.form.get("h-captcha-response", "")
                if not _verify_captcha(captcha_token):
                    flash("Verifikasi CAPTCHA gagal. Silakan coba lagi.", "danger")
                    return render_template("login.html", site_key=site_key, locked=False, skip_captcha=False, captcha_enabled=captcha_enabled)

            db = get_db()
            row = db.execute(
                "SELECT u.*, r.nama AS role FROM users u"
                " JOIN role r ON u.role_id = r.id WHERE u.username=?", (username,)
            ).fetchone()
            db.close()
            if row and check_password_hash(row["password_hash"], password):
                _clear_attempts(ip)
                is_admin = row["role"] == "admin"
                login_user(User(row["id"], row["username"], row["role"],
                                row["admin_id"], row["invite_code"]))
                log.info(f"[AUTH] Login berhasil: '{username}' (role={row['role']})")
                # Users who haven't joined a tenant yet go to the invite page.
                if not is_admin and row["admin_id"] is None:
                    dest = url_for("join")
                else:
                    dest = request.args.get("next") or url_for("dashboard")
                resp = make_response(redirect(dest))
                _set_trusted_cookie(resp)
                return resp

            # ── Login gagal ─────────────────────────────────────────────────
            count = _record_failure(ip)
            sisa  = LOGIN_MAX_ATTEMPTS - count
            log.warning(f"[AUTH] Login gagal untuk username='{username}' – percobaan ke-{count} dari IP {ip}")
            if sisa > 0:
                flash(
                    f"Username atau password salah. "
                    f"Sisa percobaan: {sisa}.",
                    "danger",
                )
            else:
                menit = LOGIN_LOCKOUT_SECS // 60
                flash(
                    f"Akun sementara dikunci selama {menit} menit karena terlalu banyak percobaan gagal.",
                    "danger",
                )
                return render_template("login.html", site_key=site_key, locked=True, skip_captcha=trusted, captcha_enabled=captcha_enabled)

        return render_template("login.html", site_key=site_key, locked=False, skip_captcha=trusted, captcha_enabled=captcha_enabled)

    @app.route("/logout")
    @login_required
    def logout():
        log.info(f"[AUTH] Logout: '{current_user.username}'")
        logout_user()
        flash("Anda berhasil logout.", "success")
        return redirect(url_for("login"))

    # ─── Kelola akun (pseudo-superadmin, link-only, di luar sistem utama) ───
    # Akses digerbang kode rahasia yang disimpan di session. TIDAK mengubah
    # tabel/role apa pun — "superadmin" hanya gerbang akses, bukan role DB.
    SUPERADMIN_CODE = "Sikal2026!"

    @app.route("/daftar-admin", methods=["GET", "POST"])
    def daftar_admin():
        if request.method == "POST":
            action = request.form.get("action", "")

            # ── Buka gerbang dengan kode rahasia ──
            if action == "unlock":
                if request.form.get("secret_code", "") == SUPERADMIN_CODE:
                    session["superadmin_ok"] = True
                return redirect(url_for("daftar_admin"))

            # ── Kunci kembali ──
            if action == "lock":
                session.pop("superadmin_ok", None)
                return redirect(url_for("daftar_admin"))

            # Aksi pengelolaan wajib sudah terbuka
            if not session.get("superadmin_ok"):
                return redirect(url_for("daftar_admin"))

            db = get_db()

            # ── Buat akun (admin atau user) ──
            if action == "create":
                username = request.form.get("username", "").strip()
                nama     = request.form.get("nama", "").strip()
                email    = request.form.get("email", "").strip()
                password = request.form.get("password", "")
                role     = request.form.get("role", "user").strip()
                admin_id = request.form.get("admin_id", type=int)

                valid_parent = (role != "user") or bool(admin_id and db.execute(
                    "SELECT id FROM users WHERE id=? AND role_id="
                    "(SELECT id FROM role WHERE nama='admin')", (admin_id,)).fetchone())

                if not username or not password:
                    flash("Username dan password wajib diisi.", "danger")
                elif len(password) < 6:
                    flash("Password minimal 6 karakter.", "danger")
                elif role not in ("admin", "user"):
                    flash("Role tidak valid.", "danger")
                elif not valid_parent:
                    flash("Pilih admin induk yang valid untuk akun user.", "danger")
                elif db.execute("SELECT id FROM users WHERE username=?", (username,)).fetchone():
                    flash("Username sudah digunakan.", "danger")
                elif role == "admin":
                    db.execute(
                        "INSERT INTO users (username, nama, email, password_hash, role_id, invite_code)"
                        " VALUES (?,?,?,?,(SELECT id FROM role WHERE nama='admin'),?)",
                        (username, nama or None, email or None,
                         generate_password_hash(password), secrets.token_urlsafe(8)))
                    db.commit()
                    flash(f'Akun admin "{username}" berhasil dibuat.', "success")
                else:
                    db.execute(
                        "INSERT INTO users (username, nama, email, password_hash, role_id, admin_id)"
                        " VALUES (?,?,?,?,(SELECT id FROM role WHERE nama='user'),?)",
                        (username, nama or None, email or None,
                         generate_password_hash(password), admin_id))
                    db.commit()
                    flash(f'Akun user "{username}" berhasil dibuat.', "success")
                db.close()
                return redirect(url_for("daftar_admin"))

            # ── Edit akun (data + reset password opsional) ──
            if action == "edit":
                uid          = request.form.get("user_id", type=int)
                username     = request.form.get("username", "").strip()
                nama         = request.form.get("nama", "").strip()
                email        = request.form.get("email", "").strip()
                new_password = request.form.get("new_password", "")
                row = db.execute("SELECT id FROM users WHERE id=?", (uid,)).fetchone() if uid else None

                if not row:
                    flash("Akun tidak ditemukan.", "danger")
                elif not username:
                    flash("Username wajib diisi.", "danger")
                elif new_password and len(new_password) < 6:
                    flash("Password baru minimal 6 karakter.", "danger")
                elif db.execute("SELECT id FROM users WHERE username=? AND id!=?",
                                (username, uid)).fetchone():
                    flash("Username sudah digunakan.", "danger")
                elif new_password:
                    db.execute(
                        "UPDATE users SET username=?, nama=?, email=?, password_hash=?,"
                        " updated_at=datetime('now') WHERE id=?",
                        (username, nama or None, email or None,
                         generate_password_hash(new_password), uid))
                    db.commit()
                    flash(f'Akun "{username}" diperbarui (password direset).', "success")
                else:
                    db.execute(
                        "UPDATE users SET username=?, nama=?, email=?,"
                        " updated_at=datetime('now') WHERE id=?",
                        (username, nama or None, email or None, uid))
                    db.commit()
                    flash(f'Akun "{username}" diperbarui.', "success")
                db.close()
                return redirect(url_for("daftar_admin"))

            # ── Hapus akun ──
            if action == "delete":
                uid = request.form.get("user_id", type=int)
                row = db.execute(
                    "SELECT u.username, r.nama AS role FROM users u"
                    " JOIN role r ON u.role_id=r.id WHERE u.id=?", (uid,)).fetchone() if uid else None
                if not row:
                    flash("Akun tidak ditemukan.", "danger")
                elif row["role"] == "admin" and db.execute(
                        "SELECT 1 FROM users WHERE admin_id=?", (uid,)).fetchone():
                    flash("Admin masih memiliki akun user. Hapus user-nya dulu.", "danger")
                elif db.execute("SELECT 1 FROM analysis_results WHERE admin_id=?", (uid,)).fetchone():
                    flash("Akun masih memiliki data analisis. Tidak dapat dihapus.", "danger")
                else:
                    db.execute("DELETE FROM users WHERE id=?", (uid,))
                    db.commit()
                    flash(f'Akun "{row["username"]}" dihapus.', "success")
                db.close()
                return redirect(url_for("daftar_admin"))

            db.close()
            return redirect(url_for("daftar_admin"))

        # ── GET ──
        if not session.get("superadmin_ok"):
            return render_template("daftar_admin.html", locked=True)

        db = get_db()

        # Sub-halaman edit/hapus satu akun (?edit=<id>)
        edit_id = request.args.get("edit", type=int)
        if edit_id:
            edit_user = db.execute(
                "SELECT u.id, u.username, u.nama, u.email, r.nama AS role,"
                " u.invite_code, u.created_at, a.username AS admin_username"
                " FROM users u JOIN role r ON u.role_id=r.id"
                " LEFT JOIN users a ON u.admin_id=a.id WHERE u.id=?", (edit_id,)).fetchone()
            db.close()
            if not edit_user:
                flash("Akun tidak ditemukan.", "danger")
                return redirect(url_for("daftar_admin"))
            return render_template("daftar_admin.html", locked=False, edit_user=edit_user)

        users = db.execute(
            "SELECT u.id, u.username, u.nama, u.email, r.nama AS role, u.admin_id,"
            " u.invite_code, u.created_at, a.username AS admin_username"
            " FROM users u JOIN role r ON u.role_id=r.id"
            " LEFT JOIN users a ON u.admin_id=a.id"
            " ORDER BY r.nama, u.username").fetchall()
        admins = db.execute(
            "SELECT id, username FROM users"
            " WHERE role_id=(SELECT id FROM role WHERE nama='admin')"
            " ORDER BY username").fetchall()
        db.close()
        return render_template("daftar_admin.html", locked=False, users=users, admins=admins)

    # ─────────────────────── pendaftaran user mandiri ─────────────────────
    @app.route("/register", methods=["GET", "POST"])
    def register():
        if current_user.is_authenticated:
            return redirect(url_for("dashboard"))

        site_key        = app.config.get("HCAPTCHA_SITE_KEY", "")
        captcha_enabled = app.config.get("HCAPTCHA_ENABLED", True)
        username = ""
        email = ""
        nama = ""
        if request.method == "POST":
            username = request.form.get("username", "").strip()
            nama     = request.form.get("nama", "").strip()
            email    = request.form.get("email", "").strip()
            password = request.form.get("password", "")
            confirm  = request.form.get("confirm_password", "")
            import re

            # ── Verifikasi CAPTCHA ──────────────────────────────────────────
            captcha_token = request.form.get("h-captcha-response", "")
            if not _verify_captcha(captcha_token):
                flash("Verifikasi CAPTCHA gagal. Silakan coba lagi.", "danger")
                return render_template("register.html", username=username,
                                       nama=nama, email=email, site_key=site_key,
                                       captcha_enabled=captcha_enabled)

            if not username or not password:
                flash("Username dan password wajib diisi.", "danger")
            elif len(username) < 8:
                flash("Username minimal 8 karakter.", "danger")
            elif not re.match(r'^[A-Za-z0-9_]+$', username):
                flash("Username hanya boleh berisi huruf, angka, dan garis bawah (_).", "danger")
            elif username[0].isdigit():
                flash("Username tidak boleh diawali dengan angka.", "danger")
            elif len(username) > 15:
                flash("Username maksimal 15 karakter.", "danger")
            elif nama and len(nama) > 255:
                flash("Nama maksimal 255 karakter.", "danger")
            elif len(password) > 255:
                flash("Password maksimal 255 karakter.", "danger")
            elif len(password) < 8:
                flash("Password minimal 8 karakter.", "danger")
            elif not re.search(r'[A-Z]', password):
                flash("Password harus mengandung minimal satu huruf kapital.", "danger")
            elif not re.search(r'[0-9]', password):
                flash("Password harus mengandung minimal satu angka.", "danger")
            elif not re.search(r'[^A-Za-z0-9]', password):
                flash("Password harus mengandung minimal satu tanda/simbol.", "danger")
            elif password != confirm:
                flash("Konfirmasi password tidak cocok.", "danger")
            else:
                db = get_db()
                if db.execute("SELECT id FROM users WHERE username=?", (username,)).fetchone():
                    db.close()
                    flash("Username sudah digunakan.", "danger")
                else:
                    db.execute(
                        "INSERT INTO users (username, nama, email, password_hash, role_id, admin_id)"
                        " VALUES (?,?,?,?,(SELECT id FROM role WHERE nama='user'),?)",
                        (username, nama or None, email, generate_password_hash(password),
                         None),
                    )
                    db.commit()
                    db.close()
                    flash("Akun berhasil dibuat. Silakan login lalu masukkan kode undangan admin.", "success")
                    return redirect(url_for("login"))

        return render_template("register.html", username=username, nama=nama,
                               email=email, site_key=site_key,
                               captcha_enabled=captcha_enabled)

    # ───────── BACKUP/DEBUG: login & register tanpa CAPTCHA (link-only) ─────────
    # Sama persis dengan /login dan /register, hanya melewati verifikasi CAPTCHA.
    @app.route("/login-no-captcha", methods=["GET", "POST"])
    def login_no_captcha():
        if current_user.is_authenticated:
            return redirect(url_for("dashboard"))

        ip                = _client_ip()
        locked, remaining = _check_lockout(ip)

        if locked:
            menit = remaining // 60
            detik = remaining % 60
            flash(
                f"Terlalu banyak percobaan login gagal. "
                f"Coba lagi dalam {menit} menit {detik} detik.",
                "danger",
            )
            return render_template("debug/login_no_captcha.html", locked=True)

        if request.method == "POST":
            username = request.form.get("username", "").strip()
            password = request.form.get("password", "")

            db = get_db()
            row = db.execute(
                "SELECT u.*, r.nama AS role FROM users u"
                " JOIN role r ON u.role_id = r.id WHERE u.username=?", (username,)
            ).fetchone()
            db.close()
            if row and check_password_hash(row["password_hash"], password):
                _clear_attempts(ip)
                is_admin = row["role"] == "admin"
                login_user(User(row["id"], row["username"], row["role"],
                                row["admin_id"], row["invite_code"]))
                log.info(f"[AUTH] Login (no-captcha) berhasil: '{username}' (role={row['role']})")
                if not is_admin and row["admin_id"] is None:
                    dest = url_for("join")
                else:
                    dest = request.args.get("next") or url_for("dashboard")
                resp = make_response(redirect(dest))
                _set_trusted_cookie(resp)
                return resp

            count = _record_failure(ip)
            sisa  = LOGIN_MAX_ATTEMPTS - count
            log.warning(f"[AUTH] Login (no-captcha) gagal untuk username='{username}' – percobaan ke-{count} dari IP {ip}")
            if sisa > 0:
                flash(f"Username atau password salah. Sisa percobaan: {sisa}.", "danger")
            else:
                menit = LOGIN_LOCKOUT_SECS // 60
                flash(f"Akun sementara dikunci selama {menit} menit karena terlalu banyak percobaan gagal.", "danger")
                return render_template("debug/login_no_captcha.html", locked=True)

        return render_template("debug/login_no_captcha.html", locked=False)

    @app.route("/register-no-captcha", methods=["GET", "POST"])
    def register_no_captcha():
        if current_user.is_authenticated:
            return redirect(url_for("dashboard"))

        username = ""
        email = ""
        nama = ""
        if request.method == "POST":
            username = request.form.get("username", "").strip()
            nama     = request.form.get("nama", "").strip()
            email    = request.form.get("email", "").strip()
            password = request.form.get("password", "")
            confirm  = request.form.get("confirm_password", "")
            import re

            if not username or not password:
                flash("Username dan password wajib diisi.", "danger")
            elif len(username) < 8:
                flash("Username minimal 8 karakter.", "danger")
            elif not re.match(r'^[A-Za-z0-9_]+$', username):
                flash("Username hanya boleh berisi huruf, angka, dan garis bawah (_).", "danger")
            elif username[0].isdigit():
                flash("Username tidak boleh diawali dengan angka.", "danger")
            elif len(username) > 15:
                flash("Username maksimal 15 karakter.", "danger")
            elif nama and len(nama) > 255:
                flash("Nama maksimal 255 karakter.", "danger")
            elif len(password) > 255:
                flash("Password maksimal 255 karakter.", "danger")
            elif len(password) < 8:
                flash("Password minimal 8 karakter.", "danger")
            elif not re.search(r'[A-Z]', password):
                flash("Password harus mengandung minimal satu huruf kapital.", "danger")
            elif not re.search(r'[0-9]', password):
                flash("Password harus mengandung minimal satu angka.", "danger")
            elif not re.search(r'[^A-Za-z0-9]', password):
                flash("Password harus mengandung minimal satu tanda/simbol.", "danger")
            elif password != confirm:
                flash("Konfirmasi password tidak cocok.", "danger")
            else:
                db = get_db()
                if db.execute("SELECT id FROM users WHERE username=?", (username,)).fetchone():
                    db.close()
                    flash("Username sudah digunakan.", "danger")
                else:
                    db.execute(
                        "INSERT INTO users (username, nama, email, password_hash, role_id, admin_id)"
                        " VALUES (?,?,?,?,(SELECT id FROM role WHERE nama='user'),?)",
                        (username, nama or None, email, generate_password_hash(password),
                         None),
                    )
                    db.commit()
                    db.close()
                    flash("Akun berhasil dibuat. Silakan login lalu masukkan kode undangan admin.", "success")
                    return redirect(url_for("login_no_captcha"))

        return render_template("debug/register_no_captcha.html", username=username,
                               nama=nama, email=email)

    # ─────────────────────── halaman masukkan kode undangan ───────────────
    @app.route("/join", methods=["GET", "POST"])
    @login_required
    def join():
        # Admins and already-joined users don't need this page
        if current_user.is_admin or not current_user.needs_invite:
            return redirect(url_for("dashboard"))

        if request.method == "POST":
            code = request.form.get("invite_code", "").strip()
            db = get_db()
            admin = db.execute(
                "SELECT id FROM users WHERE invite_code=?"
                " AND role_id=(SELECT id FROM role WHERE nama='admin')", (code,)
            ).fetchone()
            if not admin:
                db.close()
                flash("Kode undangan tidak valid.", "danger")
            else:
                db.execute(
                    "UPDATE users SET admin_id=?, updated_at=datetime('now') WHERE id=?",
                    (admin["id"], current_user.id),
                )
                db.commit()
                db.close()
                # Refresh session user object
                from auth import User as AuthUser
                updated = AuthUser(current_user.id, current_user.username, current_user.role,
                                   admin["id"], current_user.invite_code)
                login_user(updated)
                flash("Berhasil bergabung! Selamat datang.", "success")
                return redirect(url_for("dashboard"))

        return render_template("join.html")

    # ────────────────── regenerate invite code (admin only) ───────────────
    @app.route("/api/invite-code/regenerate", methods=["POST"])
    @login_required
    @admin_required
    def api_regenerate_invite_code():
        db = get_db()
        while True:
            new_code = secrets.token_urlsafe(8)
            if not db.execute("SELECT id FROM users WHERE invite_code=?", (new_code,)).fetchone():
                break
        db.execute(
            "UPDATE users SET invite_code=?, updated_at=datetime('now')"
            " WHERE id=? AND role_id=(SELECT id FROM role WHERE nama='admin')",
            (new_code, current_user.id),
        )
        db.commit()
        db.close()
        return jsonify({"success": True, "invite_code": new_code})


    # ──────────────── root redirect ──────────────────────────────────────
    @app.route("/")
    def home():
        if current_user.is_authenticated:
            return redirect(url_for("dashboard"))
        return redirect(url_for("login"))

    @app.route("/dashboard")
    @login_required
    @joined_required
    def dashboard():
        tid = current_user.tenant_id
        db = get_db()
        total = db.execute(
            "SELECT COUNT(*) FROM analysis_results WHERE admin_id=?", (tid,)
        ).fetchone()[0]
        kelas_rows = db.execute(
            """SELECT k.kode AS kelas, COUNT(*) as c
               FROM analysis_results ar JOIN kelas k ON ar.kelas_id = k.id
               WHERE ar.admin_id=? GROUP BY k.kode""",
            (tid,),
        ).fetchall()
        recent = db.execute(
            """SELECT ar.id, ar.name, k.kode AS kelas, ar.total_score, ar.created_at,
                      a.username
               FROM analysis_results ar
               JOIN users a ON ar.admin_id = a.id
               JOIN kelas k ON ar.kelas_id = k.id
               WHERE ar.admin_id=?
               ORDER BY ar.created_at DESC LIMIT 5""",
            (tid,),
        ).fetchall()
        user_count = db.execute(
            "SELECT COUNT(*) FROM users WHERE admin_id=?"
            " AND role_id=(SELECT id FROM role WHERE nama='user')", (tid,)
        ).fetchone()[0]
        db.close()
        kelas_dist = {r["kelas"]: r["c"] for r in kelas_rows}
        return render_template(
            "dashboard.html",
            total=total,
            kelas_dist=kelas_dist,
            recent=recent,
            user_count=user_count,
        )

    # ─────────────────────────── analisis (admin) ─────────────────────────
    @app.route("/analisis")
    @login_required
    @admin_required
    def analisis():
        return render_template("analisis.html")

    @app.route("/api/analyze", methods=["POST"])
    @login_required
    @admin_required
    def api_analyze():
        data = request.get_json(silent=True) or {}
        try:
            new_id, result = _perform_analysis(
                data.get("name", ""), data.get("description", ""),
                data.get("coordinates"), current_user.id,
            )
        except AnalysisError as exc:
            log.warning(f"[ANALISIS] Ditolak: {exc.message}")
            return jsonify({"error": exc.message}), exc.status
        return jsonify({"success": True, "id": new_id, "result": result})

    # ───────────────────── permintaan analisis (user → admin) ─────────────
    # User menggambar plot lahan lalu mengajukan permintaan; admin yang
    # menjalankan analisis (atau menolak). Hasil analisis tetap milik admin.

    def _permintaan_or_404(db, req_id, *, as_admin):
        """Ambil satu permintaan sesuai lingkup akun aktif, atau 404.

        Admin hanya boleh mengakses permintaan yang ditujukan ke tenant-nya;
        user hanya boleh mengakses permintaannya sendiri."""
        if as_admin:
            row = db.execute(
                "SELECT * FROM analysis_requests WHERE id=? AND admin_id=?",
                (req_id, current_user.tenant_id),
            ).fetchone()
        else:
            row = db.execute(
                "SELECT * FROM analysis_requests WHERE id=? AND user_id=?",
                (req_id, current_user.id),
            ).fetchone()
        if not row:
            db.close()
            abort(404)
        return row

    @app.route("/permintaan")
    @login_required
    @joined_required
    def permintaan():
        db = get_db()
        if current_user.is_admin:
            rows = db.execute(
                """SELECT rq.*, u.username AS requester, ar.total_score,
                          k.kode AS kelas
                   FROM analysis_requests rq
                   JOIN users u ON rq.user_id = u.id
                   LEFT JOIN analysis_results ar ON rq.result_id = ar.id
                   LEFT JOIN kelas k ON ar.kelas_id = k.id
                   WHERE rq.admin_id=?
                   ORDER BY CASE rq.status WHEN 'pending' THEN 0 ELSE 1 END,
                            rq.created_at DESC""",
                (current_user.tenant_id,),
            ).fetchall()
        else:
            rows = db.execute(
                """SELECT rq.*, u.username AS requester, ar.total_score,
                          k.kode AS kelas
                   FROM analysis_requests rq
                   JOIN users u ON rq.user_id = u.id
                   LEFT JOIN analysis_results ar ON rq.result_id = ar.id
                   LEFT JOIN kelas k ON ar.kelas_id = k.id
                   WHERE rq.user_id=?
                   ORDER BY rq.created_at DESC""",
                (current_user.id,),
            ).fetchall()
        db.close()
        return render_template("permintaan/index.html", rows=rows)

    @app.route("/permintaan/baru")
    @login_required
    @joined_required
    def permintaan_baru():
        # Admin sudah punya menu Analisis Lahan sendiri.
        if current_user.is_admin:
            return redirect(url_for("analisis"))
        return render_template("permintaan/baru.html")

    @app.route("/api/permintaan", methods=["POST"])
    @login_required
    @joined_required
    def api_permintaan_create():
        if current_user.is_admin:
            return jsonify({"error": "Admin tidak perlu mengajukan permintaan."}), 400
        data = request.get_json(silent=True) or {}
        name = data.get("name", "").strip()
        description = data.get("description", "").strip()
        coords = data.get("coordinates")

        if not name:
            return jsonify({"error": "Nama analisis wajib diisi."}), 400
        if len(name) > 255:
            return jsonify({"error": "Nama analisis maksimal 255 karakter."}), 400
        if len(description) > 1000:
            return jsonify({"error": "Deskripsi maksimal 1000 karakter."}), 400
        if not coords or len(coords) < 3:
            return jsonify({"error": "Polygon minimal 3 titik."}), 400

        # Validasi lokasi lebih awal (Indonesia & bukan perairan) agar user
        # tidak mengajukan area yang pasti akan gagal saat dianalisis admin.
        try:
            valid, reason = validate_land_area(coords)
        except Exception as exc:
            log.error(f"[PERMINTAAN] Error validasi area: {exc}")
            return jsonify({"error": _gee_error_msg(exc)}), 502
        if not valid:
            return jsonify({"error": reason}), 400

        db = get_db()
        db.execute(
            """INSERT INTO analysis_requests
               (name, description, polygon_geojson, admin_id, user_id)
               VALUES (?,?,?,?,?)""",
            (name, description, json.dumps(coords),
             current_user.tenant_id, current_user.id),
        )
        db.commit()
        db.close()
        log.info(f"[PERMINTAAN] Baru dari user '{current_user.username}': {name!r}")
        return jsonify({"success": True})

    @app.route("/api/permintaan/<int:req_id>/analisis", methods=["POST"])
    @login_required
    @admin_required
    def api_permintaan_analisis(req_id):
        db = get_db()
        req = _permintaan_or_404(db, req_id, as_admin=True)
        if req["status"] != "pending":
            db.close()
            return jsonify({"error": "Permintaan ini sudah diproses."}), 400
        requester = db.execute(
            "SELECT username FROM users WHERE id=?", (req["user_id"],)
        ).fetchone()
        coords = json.loads(req["polygon_geojson"])
        db.close()

        try:
            new_id, _ = _perform_analysis(
                req["name"], req["description"] or "", coords,
                current_user.id,
                requested_by=requester["username"] if requester else None,
            )
        except AnalysisError as exc:
            log.warning(f"[PERMINTAAN] Analisis gagal: {exc.message}")
            return jsonify({"error": exc.message}), exc.status

        db = get_db()
        db.execute(
            "UPDATE analysis_requests SET status='approved', result_id=?,"
            " updated_at=datetime('now') WHERE id=?",
            (new_id, req_id),
        )
        db.commit()
        db.close()
        return jsonify({"success": True, "id": new_id})

    @app.route("/api/permintaan/<int:req_id>/tolak", methods=["POST"])
    @login_required
    @admin_required
    def api_permintaan_tolak(req_id):
        note = ((request.get_json(silent=True) or {}).get("note") or "").strip()
        db = get_db()
        req = _permintaan_or_404(db, req_id, as_admin=True)
        if req["status"] != "pending":
            db.close()
            return jsonify({"error": "Permintaan ini sudah diproses."}), 400
        db.execute(
            "UPDATE analysis_requests SET status='rejected', admin_note=?,"
            " updated_at=datetime('now') WHERE id=?",
            (note[:500], req_id),
        )
        db.commit()
        db.close()
        log.info(f"[PERMINTAAN] Ditolak (ID={req_id}) oleh '{current_user.username}'")
        return jsonify({"success": True})

    @app.route("/api/permintaan/<int:req_id>/batal", methods=["POST"])
    @login_required
    @joined_required
    def api_permintaan_batal(req_id):
        db = get_db()
        req = _permintaan_or_404(db, req_id, as_admin=False)
        if req["status"] != "pending":
            db.close()
            return jsonify({"error": "Hanya permintaan yang masih menunggu dapat dibatalkan."}), 400
        db.execute("DELETE FROM analysis_requests WHERE id=?", (req_id,))
        db.commit()
        db.close()
        return jsonify({"success": True})

    # ──────────────────────── informasi kesesuaian ────────────────────────
    @app.route("/informasi")
    @login_required
    @joined_required
    def informasi():
        tid = current_user.tenant_id
        db = get_db()
        rows = db.execute(
            """SELECT ar.id, ar.name, k.kode AS kelas, ar.total_score,
                      ar.description, ar.created_at, a.username
               FROM analysis_results ar
               JOIN users a ON ar.admin_id = a.id
               JOIN kelas k ON ar.kelas_id = k.id
               WHERE ar.admin_id=?
               ORDER BY ar.created_at DESC""",
            (tid,),
        ).fetchall()
        db.close()
        return render_template("informasi/index.html", rows=rows)

    @app.route("/informasi/<int:result_id>")
    @login_required
    @joined_required
    def informasi_detail(result_id):
        tid = current_user.tenant_id
        db = get_db()
        row = db.execute(
            """SELECT ar.*, a.username
               FROM analysis_results ar
               JOIN users a ON ar.admin_id = a.id
               WHERE ar.id=? AND ar.admin_id=?""",
            (result_id, tid),
        ).fetchone()
        db.close()
        if not row:
            abort(404)
        raw = json.loads(row["raw_params"]) if row["raw_params"] else {}
        saw = calculate_saw(raw)   # Always recalculate fresh from stored raw params
        resp = make_response(render_template(
            "informasi/detail.html",
            row=row,
            saw=saw,
            raw=raw,
            polygon=json.loads(row["polygon_geojson"]),
            criteria_labels=CRITERIA_LABELS,
            criteria=CRITERIA,
            kelas_info=get_kelas_map(),
        ))
        resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
        resp.headers["Pragma"] = "no-cache"
        return resp

    @app.route("/informasi/<int:result_id>/pdf")
    @login_required
    @joined_required
    def informasi_pdf(result_id):
        tid = current_user.tenant_id
        db = get_db()
        row = db.execute(
            """SELECT ar.*, a.username
               FROM analysis_results ar
               JOIN users a ON ar.admin_id = a.id
               WHERE ar.id=? AND ar.admin_id=?""",
            (result_id, tid),
        ).fetchone()
        db.close()
        if not row:
            abort(404)
        raw = json.loads(row["raw_params"]) if row["raw_params"] else {}
        saw = calculate_saw(raw)
        pdf_bytes = build_analysis_pdf(row, saw, raw, kelas_info=get_kelas_map())

        # Nama file aman dari karakter filesystem
        safe_name = "".join(
            c if c.isalnum() or c in (" ", "-", "_") else "_"
            for c in (row["name"] or "analisis")
        ).strip().replace(" ", "_") or "analisis"
        filename = f"Laporan_{safe_name}.pdf"

        resp = make_response(pdf_bytes)
        resp.headers["Content-Type"] = "application/pdf"
        resp.headers["Content-Disposition"] = f'attachment; filename="{filename}"'
        resp.headers["Cache-Control"] = "no-store"
        return resp

    @app.route("/informasi/<int:result_id>/edit", methods=["GET", "POST"])
    @login_required
    @admin_required
    def informasi_edit(result_id):
        db = get_db()
        row = db.execute(
            "SELECT * FROM analysis_results WHERE id=? AND admin_id=?",
            (result_id, current_user.id),
        ).fetchone()
        db.close()
        if not row:
            abort(404)

        if request.method == "POST":
            name = request.form.get("name", "").strip()
            description = request.form.get("description", "").strip()
            if not name:
                flash("Nama analisis wajib diisi.", "danger")
                return redirect(url_for("informasi_edit", result_id=result_id))
            if len(name) > 255:
                flash("Nama analisis maksimal 255 karakter.", "danger")
                return redirect(url_for("informasi_edit", result_id=result_id))
            if len(description) > 1000:
                flash("Deskripsi maksimal 1000 karakter.", "danger")
                return redirect(url_for("informasi_edit", result_id=result_id))

            # Parse submitted raw parameter values and recalculate SAW
            _raw_fields = [
                "slope", "drainage", "soil_depth", "soil_texture", "soil_type",
                "lulc", "precipitation", "temperature", "distance_road", "distance_river",
            ]
            existing_raw = json.loads(row["raw_params"]) if row["raw_params"] else {}
            new_raw = {}
            for key in _raw_fields:
                val_str = request.form.get(f"raw_{key}", "").strip()
                if val_str != "":
                    try:
                        new_raw[key] = float(val_str)
                    except ValueError:
                        new_raw[key] = existing_raw.get(key)
                else:
                    new_raw[key] = existing_raw.get(key)

            new_saw = calculate_saw(new_raw)

            # Cek apakah ada perubahan
            old_name = (row["name"] or "").strip()
            old_desc = (row["description"] or "").strip()
            old_raw  = json.loads(row["raw_params"]) if row["raw_params"] else {}

            def _raw_equal(a, b):
                if set(a.keys()) != set(b.keys()):
                    return False
                for k in a:
                    av, bv = a[k], b[k]
                    if av is None and bv is None:
                        continue
                    if av is None or bv is None:
                        return False
                    if round(float(av), 6) != round(float(bv), 6):
                        return False
                return True

            no_change = (name == old_name and description == old_desc and _raw_equal(new_raw, old_raw))

            if no_change:
                flash("Tidak ada perubahan yang disimpan.", "info")
                return redirect(url_for("informasi_detail", result_id=result_id))

            db = get_db()
            db.execute(
                """UPDATE analysis_results
                   SET name=?, description=?, raw_params=?, saw_result=?,
                       kelas_id=(SELECT id FROM kelas WHERE kode=?),
                       total_score=?, updated_at=datetime('now')
                   WHERE id=?""",
                (
                    name, description,
                    json.dumps(new_raw), json.dumps(new_saw),
                    new_saw["kelas"], new_saw["total"],
                    result_id,
                ),
            )
            db.commit()
            db.close()

            # Simpan log perubahan nama/deskripsi
            _edit_logs: list = []
            now_str = datetime.now().strftime('%H:%M:%S')
            _edit_logs.append({'time': now_str, 'level': 'INFO',  'message': '──────────────────────────────────────────────────'})
            _edit_logs.append({'time': now_str, 'level': 'INFO',  'message': f"[EDIT] Diperbarui oleh '{current_user.username}'"})
            if name != old_name:
                _edit_logs.append({'time': now_str, 'level': 'INFO', 'message': f"[EDIT] Nama    : {old_name!r} → {name!r}"})
            else:
                _edit_logs.append({'time': now_str, 'level': 'INFO', 'message': f"[EDIT] Nama    : {name!r} (tidak berubah)"})
            old_desc_val = old_desc if old_desc else '(kosong)'
            new_desc_val = description if description else '(kosong)'
            if description != old_desc:
                _edit_logs.append({'time': now_str, 'level': 'INFO', 'message': f"[EDIT] Deskripsi : {old_desc_val!r} → {new_desc_val!r}"})
            else:
                _edit_logs.append({'time': now_str, 'level': 'INFO', 'message': f"[EDIT] Deskripsi : {new_desc_val!r} (tidak berubah)"})
            if not _raw_equal(new_raw, old_raw):
                _edit_logs.append({'time': now_str, 'level': 'INFO', 'message': '[EDIT] Parameter mentah diperbarui, SAW dihitung ulang'})
                _edit_logs.append({'time': now_str, 'level': 'INFO', 'message': f"[SAW] Kelas: {new_saw['kelas']}  |  Skor: {new_saw['total']:.4f}"})
            _edit_logs.append({'time': now_str, 'level': 'INFO', 'message': '──────────────────────────────────────────────────'})

            db2 = get_db()
            max_run = db2.execute(
                "SELECT COALESCE(MAX(run_number), 0) FROM analysis_logs WHERE result_id=?",
                (result_id,)
            ).fetchone()[0]
            db2.close()
            _save_logs(result_id, max_run + 1, _edit_logs)

            flash("Data analisis berhasil diperbarui.", "success")
            return redirect(url_for("informasi_detail", result_id=result_id, nocache=int(time.time())))

        return render_template(
            "informasi/edit.html",
            row=row,
            saw=calculate_saw(json.loads(row["raw_params"]) if row["raw_params"] else {}),
            raw_params=json.loads(row["raw_params"]) if row["raw_params"] else {},
            polygon=json.loads(row["polygon_geojson"]),
            criteria=CRITERIA,
            criteria_labels=CRITERIA_LABELS,
        )

    @app.route("/api/analysis/<int:result_id>/reanalyze", methods=["POST"])
    @login_required
    @admin_required
    def api_reanalyze(result_id):
        db = get_db()
        row = db.execute(
            "SELECT * FROM analysis_results WHERE id=? AND admin_id=?",
            (result_id, current_user.id),
        ).fetchone()
        db.close()
        if not row:
            return jsonify({"error": "Data tidak ditemukan."}), 404

        data = request.get_json(silent=True) or {}
        coords = data.get("coordinates") or json.loads(row["polygon_geojson"])

        _captured: list = []
        _handler = _CaptureHandler(_captured)
        logging.getLogger('sipadi').addHandler(_handler)
        try:
            t_start = time.perf_counter()
            log.info("──────────────────────────────────────────────────")
            log.info(f"[ANALISIS] Tanggal : {_tanggal_id()}", extra=DISPLAY)
            log.info(f"[ANALISIS] Re-analisis ID={result_id} oleh '{current_user.username}'", extra=DISPLAY)
            log.info(f"[ANALISIS] Nama    : {row['name']!r}", extra=DISPLAY)
            desc = (row['description'] or '').strip()
            log.info(f"[ANALISIS] Deskripsi : {desc!r}" if desc else "[ANALISIS] Deskripsi : (kosong)", extra=DISPLAY)
            log.info(f"[ANALISIS] Titik   : {len(coords)} koordinat", extra=DISPLAY)

            log.info("[GEE] Mengambil 10 parameter dari Google Earth Engine…")
            t0 = time.perf_counter()
            try:
                raw = fetch_all_parameters(coords)
                elapsed = time.perf_counter() - t0
                log.info(f"[GEE] Semua parameter berhasil diambil ({elapsed:.1f}s)")
                for k, v in raw.items():
                    log.debug(f"[GEE]   {k:<20} = {v}")
            except Exception as exc:
                log.error(f"[GEE] Error saat re-analisis: {exc}")
                return jsonify({"error": _gee_error_msg(exc)}), 502

            log.info("[SAW] Menghitung SAW…")
            result = calculate_saw(raw)
            log.info(f"[SAW] Kelas: {result['kelas']}  |  Skor: {result['total']:.4f}", extra=DISPLAY)

            log.info("[DB] Memperbarui hasil di database…")
            db = get_db()
            db.execute(
                """UPDATE analysis_results
                   SET polygon_geojson=?, raw_params=?, saw_result=?,
                       kelas_id=(SELECT id FROM kelas WHERE kode=?),
                       total_score=?, updated_at=datetime('now')
                   WHERE id=?""",
                (
                    json.dumps(coords),
                    json.dumps(raw),
                    json.dumps(result),
                    result["kelas"],
                    result["total"],
                    result_id,
                ),
            )
            db.commit()
            db.close()
            total_elapsed = time.perf_counter() - t_start
            log.info(f"[ANALISIS] Re-analisis selesai dalam {total_elapsed:.1f}s → Kelas={result['kelas']}")
            log.info("──────────────────────────────────────────────────")
        finally:
            logging.getLogger('sipadi').removeHandler(_handler)

        db = get_db()
        max_run = db.execute(
            "SELECT COALESCE(MAX(run_number), 0) FROM analysis_logs WHERE result_id=?",
            (result_id,)
        ).fetchone()[0]
        db.close()
        _save_logs(result_id, max_run + 1, _captured)
        return jsonify({"success": True, "result": result})

    @app.route("/api/analysis/<int:result_id>/override-params", methods=["POST"])
    @login_required
    @admin_required
    def api_override_params(result_id):
        """Override satu atau lebih nilai mentah parameter, lalu hitung ulang SAW."""
        db = get_db()
        row = db.execute(
            "SELECT * FROM analysis_results WHERE id=? AND admin_id=?",
            (result_id, current_user.id),
        ).fetchone()
        db.close()
        if not row:
            return jsonify({"error": "Data tidak ditemukan."}), 404

        data = request.get_json(silent=True) or {}
        overrides = data.get("overrides", {})
        if not isinstance(overrides, dict) or not overrides:
            return jsonify({"error": "Payload overrides tidak valid."}), 400

        # Merge overrides into existing raw params
        raw = json.loads(row["raw_params"])
        for key, val in overrides.items():
            if key not in CRITERIA:
                continue
            try:
                raw[key] = float(val)
            except (ValueError, TypeError):
                pass  # ignore unparseable values

        result = calculate_saw(raw)
        db = get_db()
        db.execute(
            """UPDATE analysis_results
               SET raw_params=?, saw_result=?,
                   kelas_id=(SELECT id FROM kelas WHERE kode=?), total_score=?,
                   updated_at=datetime('now')
               WHERE id=?""",
            (json.dumps(raw), json.dumps(result), result["kelas"], result["total"], result_id),
        )
        db.commit()
        db.close()
        return jsonify({"success": True, "result": result, "raw": raw})

    @app.route("/api/analysis/all-geojson")
    @login_required
    def api_all_geojson():
        tid = current_user.tenant_id
        db = get_db()
        rows = db.execute(
            """SELECT ar.id, ar.name, k.kode AS kelas, ar.total_score, ar.polygon_geojson
               FROM analysis_results ar
               JOIN kelas k ON ar.kelas_id = k.id
               WHERE ar.admin_id=?
               ORDER BY ar.created_at DESC""",
            (tid,),
        ).fetchall()
        db.close()
        features = []
        for r in rows:
            try:
                coords = json.loads(r["polygon_geojson"])
            except Exception:
                continue
            features.append({
                "type": "Feature",
                "geometry": {"type": "Polygon", "coordinates": [coords]},
                "properties": {
                    "id": r["id"],
                    "name": r["name"],
                    "kelas": r["kelas"],
                    "total_score": round(r["total_score"], 4),
                    "detail_url": url_for("informasi_detail", result_id=r["id"]),
                },
            })
        return jsonify({"type": "FeatureCollection", "features": features})

    @app.route("/api/analysis/<int:result_id>/logs")
    @login_required
    @joined_required
    def api_analysis_logs(result_id):
        tid = current_user.tenant_id
        db = get_db()
        row = db.execute(
            "SELECT id FROM analysis_results WHERE id=? AND admin_id=?", (result_id, tid)
        ).fetchone()
        if not row:
            db.close()
            return jsonify({"error": "Tidak ditemukan."}), 404

        run = request.args.get('run', type=int)
        if run is None:
            runs = db.execute(
                "SELECT DISTINCT run_number FROM analysis_logs WHERE result_id=? ORDER BY run_number",
                (result_id,)
            ).fetchall()
            db.close()
            return jsonify({"runs": [r["run_number"] for r in runs]})

        logs = db.execute(
            "SELECT logged_at, level, message FROM analysis_logs"
            " WHERE result_id=? AND run_number=? ORDER BY id",
            (result_id, run)
        ).fetchall()
        db.close()
        return jsonify({"logs": [dict(r) for r in logs]})

    @app.route("/api/analysis/<int:result_id>/delete", methods=["POST"])
    @login_required
    @admin_required
    def api_delete_analysis(result_id):
        db = get_db()
        # Only delete if owned by this admin's tenant
        result = db.execute(
            "SELECT id FROM analysis_results WHERE id=? AND admin_id=?",
            (result_id, current_user.id),
        ).fetchone()
        if not result:
            db.close()
            return jsonify({"error": "Tidak ditemukan."}), 404
        db.execute("DELETE FROM analysis_results WHERE id=?", (result_id,))
        db.commit()
        db.close()
        return jsonify({"success": True})

    # ──────────────────────────── akun saya ───────────────────────────────
    @app.route("/akun", methods=["GET", "POST"])
    @login_required
    @joined_required
    def akun():
        db = get_db()
        row = db.execute(
            "SELECT u.*, r.nama AS role FROM users u"
            " JOIN role r ON u.role_id = r.id WHERE u.id=?", (current_user.id,)
        ).fetchone()
        db.close()
        user = dict(row)

        if request.method == "POST":
            username = request.form.get("username", "").strip()
            nama = request.form.get("nama", "").strip()
            email = request.form.get("email", "").strip()
            cur_pw = request.form.get("current_password", "")
            new_pw = request.form.get("new_password", "")
            confirm_pw = request.form.get("confirm_password", "")

            if not username:
                flash("Username tidak boleh kosong.", "danger")
                return redirect(url_for("akun"))
            if len(username) > 15:
                flash("Username maksimal 15 karakter.", "danger")
                return redirect(url_for("akun"))
            if len(nama) > 255:
                flash("Nama maksimal 255 karakter.", "danger")
                return redirect(url_for("akun"))
            if len(email) > 255:
                flash("Email maksimal 255 karakter.", "danger")
                return redirect(url_for("akun"))

            # Validasi format username jika berubah
            if username != user["username"]:
                import re as _re
                if len(username) < 8:
                    flash("Username minimal 8 karakter.", "danger")
                    return redirect(url_for("akun"))
                if not _re.match(r'^[A-Za-z0-9_]+$', username):
                    flash("Username hanya boleh berisi huruf, angka, dan garis bawah (_).", "danger")
                    return redirect(url_for("akun"))
                if username[0].isdigit():
                    flash("Username tidak boleh diawali dengan angka.", "danger")
                    return redirect(url_for("akun"))

            # Verifikasi password saat ini hanya jika ada password baru
            if new_pw:
                if not cur_pw or not check_password_hash(user["password_hash"], cur_pw):
                    flash("Password saat ini wajib diisi dan harus benar untuk mengubah password.", "danger")
                    return redirect(url_for("akun"))

            db = get_db()
            # Check username uniqueness (excluding current user)
            taken = db.execute(
                "SELECT id FROM users WHERE username=? AND id!=?",
                (username, current_user.id),
            ).fetchone()
            if taken:
                db.close()
                flash("Username sudah digunakan.", "danger")
                return redirect(url_for("akun"))

            # Cek apakah ada perubahan
            no_change = (
                username == user["username"]
                and (nama or None) == user["nama"]
                and (email or None) == user["email"]
                and not new_pw
            )
            if no_change:
                db.close()
                flash("Tidak ada perubahan yang disimpan.", "info")
                return redirect(url_for("akun"))

            if new_pw:
                if new_pw != confirm_pw:
                    db.close()
                    flash("Konfirmasi password baru tidak cocok.", "danger")
                    return redirect(url_for("akun"))
                import re as _re
                if len(new_pw) > 255:
                    db.close()
                    flash("Password baru maksimal 255 karakter.", "danger")
                    return redirect(url_for("akun"))
                if len(new_pw) < 8:
                    db.close()
                    flash("Password baru minimal 8 karakter.", "danger")
                    return redirect(url_for("akun"))
                if not _re.search(r'[A-Z]', new_pw):
                    db.close()
                    flash("Password baru harus mengandung huruf kapital.", "danger")
                    return redirect(url_for("akun"))
                if not _re.search(r'[0-9]', new_pw):
                    db.close()
                    flash("Password baru harus mengandung angka.", "danger")
                    return redirect(url_for("akun"))
                if not _re.search(r'[^A-Za-z0-9]', new_pw):
                    db.close()
                    flash("Password baru harus mengandung simbol.", "danger")
                    return redirect(url_for("akun"))
                db.execute(
                    """UPDATE users SET username=?, nama=?, email=?, password_hash=?,
                       updated_at=datetime('now') WHERE id=?""",
                    (username, nama or None, email, generate_password_hash(new_pw), current_user.id),
                )
            else:
                db.execute(
                    """UPDATE users SET username=?, nama=?, email=?,
                       updated_at=datetime('now') WHERE id=?""",
                    (username, nama or None, email, current_user.id),
                )
            db.commit()
            db.close()
            flash("Profil berhasil diperbarui.", "success")
            return redirect(url_for("akun"))

        return render_template("akun.html", user=user)

    # ─────────────────────── admin – kelola akun ──────────────────────────
    @app.route("/kelola", methods=["GET", "POST"])
    @app.route("/kelola/edit/<int:user_id>", methods=["GET", "POST"], endpoint="kelola_edit")
    @app.route("/kelola/buat", methods=["GET", "POST"], endpoint="kelola_buat")
    @login_required
    @admin_required
    def buat_akun(user_id=None):
        db = get_db()

        # Mode ditentukan oleh endpoint / path-based URL
        create_mode = request.endpoint == "kelola_buat"
        edit_user_id = user_id
        edit_user = None

        if edit_user_id:
            edit_user = db.execute(
                "SELECT id, username, nama, email, created_at, admin_id FROM users WHERE id=?",
                (edit_user_id,)
            ).fetchone()
            if not edit_user or edit_user["admin_id"] != current_user.id:
                db.close()
                flash("Pengguna tidak ditemukan.", "danger")
                return redirect(url_for("buat_akun"))

        # Handle POST (create user or edit user)
        if request.method == "POST":
            username = request.form.get("username", "").strip()
            email = request.form.get("email", "").strip()
            nama = request.form.get("nama", "").strip()
            mode = request.form.get("mode", "").strip()

            # Determine if this is edit or create
            post_edit_user_id = request.form.get("user_id", type=int)

            if post_edit_user_id:
                # ─── EDIT USER ───
                new_password = request.form.get("new_password", "").strip()
                confirm_password = request.form.get("confirm_password", "").strip()

                if not username:
                    db.close()
                    flash("Username wajib diisi.", "danger")
                    return redirect(url_for("kelola_edit", user_id=post_edit_user_id))
                if len(username) > 15:
                    db.close()
                    flash("Username maksimal 15 karakter.", "danger")
                    return redirect(url_for("kelola_edit", user_id=post_edit_user_id))
                if len(nama) > 255:
                    db.close()
                    flash("Nama maksimal 255 karakter.", "danger")
                    return redirect(url_for("kelola_edit", user_id=post_edit_user_id))
                if len(email) > 255:
                    db.close()
                    flash("Email maksimal 255 karakter.", "danger")
                    return redirect(url_for("kelola_edit", user_id=post_edit_user_id))

                # Validasi format username jika berubah
                if edit_user and username != edit_user["username"]:
                    import re as _re
                    if len(username) < 8:
                        db.close()
                        flash("Username minimal 8 karakter.", "danger")
                        return redirect(url_for("kelola_edit", user_id=post_edit_user_id))
                    if not _re.match(r'^[A-Za-z0-9_]+$', username):
                        db.close()
                        flash("Username hanya boleh berisi huruf, angka, dan garis bawah (_).", "danger")
                        return redirect(url_for("kelola_edit", user_id=post_edit_user_id))
                    if username[0].isdigit():
                        db.close()
                        flash("Username tidak boleh diawali dengan angka.", "danger")
                        return redirect(url_for("kelola_edit", user_id=post_edit_user_id))

                # Check username uniqueness (exclude current user)
                existing = db.execute(
                    "SELECT id FROM users WHERE username=? AND id!=?",
                    (username, post_edit_user_id)
                ).fetchone()
                if existing:
                    db.close()
                    flash("Username sudah digunakan.", "danger")
                    return redirect(url_for("kelola_edit", user_id=post_edit_user_id))

                # Cek apakah ada perubahan
                no_change = (
                    edit_user
                    and username == edit_user["username"]
                    and (nama or None) == edit_user["nama"]
                    and (email or None) == edit_user["email"]
                    and not new_password
                )
                if no_change:
                    db.close()
                    flash("Tidak ada perubahan yang disimpan.", "info")
                    return redirect(url_for("kelola_edit", user_id=post_edit_user_id))

                # Check password match if provided
                if new_password or confirm_password:
                    if new_password != confirm_password:
                        db.close()
                        flash("Konfirmasi password tidak cocok.", "danger")
                        return redirect(url_for("kelola_edit", user_id=post_edit_user_id))
                    import re as _re
                    if len(new_password) > 255:
                        db.close()
                        flash("Password maksimal 255 karakter.", "danger")
                        return redirect(url_for("kelola_edit", user_id=post_edit_user_id))
                    if len(new_password) < 8:
                        db.close()
                        flash("Password minimal 8 karakter.", "danger")
                        return redirect(url_for("kelola_edit", user_id=post_edit_user_id))
                    if not _re.search(r'[A-Z]', new_password):
                        db.close()
                        flash("Password harus mengandung huruf kapital.", "danger")
                        return redirect(url_for("kelola_edit", user_id=post_edit_user_id))
                    if not _re.search(r'[0-9]', new_password):
                        db.close()
                        flash("Password harus mengandung angka.", "danger")
                        return redirect(url_for("kelola_edit", user_id=post_edit_user_id))
                    if not _re.search(r'[^A-Za-z0-9]', new_password):
                        db.close()
                        flash("Password harus mengandung simbol.", "danger")
                        return redirect(url_for("kelola_edit", user_id=post_edit_user_id))

                    # Update with new password
                    db.execute(
                        "UPDATE users SET username=?, nama=?, email=?, password_hash=? WHERE id=?",
                        (username, nama or None, email or None, generate_password_hash(new_password), post_edit_user_id)
                    )
                else:
                    # Update without password
                    db.execute(
                        "UPDATE users SET username=?, nama=?, email=? WHERE id=?",
                        (username, nama or None, email or None, post_edit_user_id)
                    )

                db.commit()
                log.info(f"[ADMIN] User {post_edit_user_id} updated by {current_user.username}")
                db.close()
                flash("Pengguna berhasil diperbarui.", "success")
                return redirect(url_for("buat_akun"))

            elif mode == "create":
                # ─── CREATE USER (from separate form) ───
                password = request.form.get("password", "").strip()
                confirm = request.form.get("confirm_password", "").strip()

                if not username:
                    flash("Username wajib diisi.", "danger")
                    return redirect(url_for("kelola_buat"))
                import re as _re
                if len(username) < 8:
                    flash("Username minimal 8 karakter.", "danger")
                    return redirect(url_for("kelola_buat"))
                if not _re.match(r'^[A-Za-z0-9_]+$', username):
                    flash("Username hanya boleh berisi huruf, angka, dan garis bawah (_).", "danger")
                    return redirect(url_for("kelola_buat"))
                if username[0].isdigit():
                    flash("Username tidak boleh diawali dengan angka.", "danger")
                    return redirect(url_for("kelola_buat"))
                if len(username) > 15:
                    flash("Username maksimal 15 karakter.", "danger")
                    return redirect(url_for("kelola_buat"))
                if nama and len(nama) > 255:
                    flash("Nama maksimal 255 karakter.", "danger")
                    return redirect(url_for("kelola_buat"))
                if email and len(email) > 255:
                    flash("Email maksimal 255 karakter.", "danger")
                    return redirect(url_for("kelola_buat"))

                auto_password = False
                if not password:
                    # Generate password otomatis jika dikosongkan
                    password = secrets.token_urlsafe(8)
                    auto_password = True
                else:
                    if password != confirm:
                        flash("Konfirmasi password tidak cocok.", "danger")
                        return redirect(url_for("kelola_buat"))
                    if len(password) > 255:
                        flash("Password maksimal 255 karakter.", "danger")
                        return redirect(url_for("kelola_buat"))
                    if len(password) < 8:
                        flash("Password minimal 8 karakter.", "danger")
                        return redirect(url_for("kelola_buat"))
                    if not _re.search(r'[A-Z]', password):
                        flash("Password harus mengandung huruf kapital.", "danger")
                        return redirect(url_for("kelola_buat"))
                    if not _re.search(r'[0-9]', password):
                        flash("Password harus mengandung angka.", "danger")
                        return redirect(url_for("kelola_buat"))
                    if not _re.search(r'[^A-Za-z0-9]', password):
                        flash("Password harus mengandung simbol.", "danger")
                        return redirect(url_for("kelola_buat"))

                if db.execute("SELECT id FROM users WHERE username=?", (username,)).fetchone():
                    db.close()
                    flash("Username sudah digunakan.", "danger")
                    return redirect(url_for("kelola_buat"))

                # Create as 'user' belonging to current admin's tenant
                db.execute(
                    "INSERT INTO users (username, nama, email, password_hash, role_id, admin_id)"
                    " VALUES (?,?,?,?,(SELECT id FROM role WHERE nama='user'),?)",
                    (username, nama or None, email or None, generate_password_hash(password),
                     current_user.id),
                )
                db.commit()
                log.info(f"[ADMIN] New user {username} created by {current_user.username}")
                db.close()
                if auto_password:
                    flash(f'Akun "{username}" berhasil dibuat. Password otomatis: {password}', "success")
                else:
                    flash(f'Akun "{username}" berhasil dibuat.', "success")
                return redirect(url_for("buat_akun"))

        # GET: render template berdasarkan mode
        if create_mode:
            db.close()
            return render_template("kelola/buat.html")
        if edit_user:
            db.close()
            return render_template("kelola/edit.html", edit_user=edit_user)

        # Default: daftar pengguna
        users = db.execute(
            "SELECT u.id, u.username, u.nama, u.email, r.nama AS role, u.created_at"
            " FROM users u JOIN role r ON u.role_id = r.id"
            " WHERE u.admin_id=? AND u.role_id=(SELECT id FROM role WHERE nama='user')"
            " ORDER BY u.created_at DESC",
            (current_user.id,),
        ).fetchall()
        invite_code = db.execute(
            "SELECT invite_code FROM users WHERE id=?", (current_user.id,)
        ).fetchone()["invite_code"]
        db.close()
        return render_template("kelola/index.html", users=users, invite_code=invite_code)

    # ─────────────────────── admin – delete user ───────────────────────────
    @app.route("/api/admin/user/<int:user_id>/delete", methods=["POST"])
    @login_required
    @admin_required
    def delete_user(user_id):
        """Delete user (admin only)."""
        db = get_db()
        try:
            # Verify user belongs to current admin's tenant
            user = db.execute(
                "SELECT username, admin_id FROM users WHERE id=?", (user_id,)
            ).fetchone()
            if not user or user["admin_id"] != current_user.id:
                db.close()
                return jsonify({"error": "Pengguna tidak ditemukan atau tidak memiliki akses."}), 403

            # Delete user
            db.execute("DELETE FROM users WHERE id=?", (user_id,))
            db.commit()
            log.info(f"[ADMIN] User {user['username']} (id={user_id}) deleted by {current_user.username}")
            return jsonify({"success": True}), 200
        except Exception as e:
            db.rollback()
            log.error(f"[ERROR] Delete user failed: {e}")
            return jsonify({"error": "Terjadi kesalahan saat menghapus."}), 500
        finally:
            db.close()

    # ─────────────────────────── error handlers ───────────────────────────
    @app.errorhandler(403)
    def forbidden(e):
        return redirect(url_for('dashboard'))

    @app.errorhandler(404)
    def not_found(e):
        if current_user.is_authenticated:
            return redirect(url_for("dashboard"))
        return redirect(url_for("login"))

    return app


if __name__ == "__main__":
    app = create_app()
    app.run(debug=True, port=5000)
