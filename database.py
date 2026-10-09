import os
import secrets
import sqlite3

from werkzeug.security import generate_password_hash

from config import Config


def get_db():
    """Return a new database connection."""
    os.makedirs(os.path.dirname(Config.DATABASE), exist_ok=True)
    conn = sqlite3.connect(Config.DATABASE)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def get_kelas_map() -> dict:
    """Kembalikan data master kelas sebagai dict berkunci kode (S1/S2/S3/N).

    Dipakai template & PDF agar penjelasan kelas bersumber dari tabel ``kelas``
    (ternormalisasi), bukan ditulis ulang (hardcode) di tiap tampilan.
    """
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM kelas ORDER BY id"
    ).fetchall()
    conn.close()
    return {r["kode"]: dict(r) for r in rows}


def _unique_invite_code(conn) -> str:
    """Return an invite code not yet used by any account."""
    while True:
        code = secrets.token_urlsafe(8)
        if not conn.execute(
            "SELECT id FROM users WHERE invite_code=?", (code,)
        ).fetchone():
            return code


def init_db():
    """Create the unified-account schema and seed the default admin.

    Accounts (admin and user) live in a single ``users`` table distinguished
    by a ``role`` column. Admins own an ``invite_code``; users have an
    ``admin_id`` pointing to the admin (tenant) they joined. Older schemas
    (two separate ``admins``/``users`` tables, or the very old ``password``
    column) are migrated automatically.
    """
    conn = get_db()

    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    )}

    # ── Migration 1: very old schema used 'password' instead of 'password_hash' ──
    if "users" in tables:
        old_cols = {r[1] for r in conn.execute("PRAGMA table_info(users)")}
        if "password" in old_cols and "password_hash" not in old_cols:
            conn.close()
            os.remove(Config.DATABASE)
            conn = get_db()
            tables = set()

    # ── Migration 2: two-table schema ('admins' + 'users') → unified 'users' ─────
    if "admins" in tables:
        # Ensure the optional columns exist on the legacy tables before copying.
        admin_cols = {r[1] for r in conn.execute("PRAGMA table_info(admins)")}
        for col, typedef in [("email", "TEXT"), ("nama", "TEXT"),
                             ("invite_code", "TEXT"),
                             ("updated_at", "TIMESTAMP DEFAULT (datetime('now'))")]:
            if col not in admin_cols:
                conn.execute(f"ALTER TABLE admins ADD COLUMN {col} {typedef}")
        if "users" in tables:
            user_cols = {r[1] for r in conn.execute("PRAGMA table_info(users)")}
            for col, typedef in [("email", "TEXT"), ("nama", "TEXT"),
                                 ("admin_id", "INTEGER"),
                                 ("updated_at", "TIMESTAMP DEFAULT (datetime('now'))")]:
                if col not in user_cols:
                    conn.execute(f"ALTER TABLE users ADD COLUMN {col} {typedef}")
        conn.commit()

        remap_results = "analysis_results" in tables and "admin_id" in {
            r[1] for r in conn.execute("PRAGMA table_info(analysis_results)")
        }

        # New unified table. New ids are assigned automatically; a temporary map
        # translates old admin ids → new ids so foreign keys stay valid.
        merge_sql = """
            CREATE TABLE _users_unified (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                username      TEXT    NOT NULL UNIQUE,
                password_hash TEXT    NOT NULL,
                email         TEXT,
                nama          TEXT,
                role          TEXT    NOT NULL DEFAULT 'user',
                admin_id      INTEGER REFERENCES _users_unified(id),
                invite_code   TEXT    UNIQUE,
                created_at    TIMESTAMP DEFAULT (datetime('now')),
                updated_at    TIMESTAMP DEFAULT (datetime('now'))
            );

            INSERT INTO _users_unified
                (username, password_hash, email, nama, role,
                 admin_id, invite_code, created_at, updated_at)
            SELECT username, password_hash, email, nama, 'admin',
                   NULL, invite_code, created_at, updated_at
            FROM admins;

            CREATE TEMP TABLE _admin_map AS
            SELECT a.id AS old_id, u.id AS new_id
            FROM admins a JOIN _users_unified u ON u.username = a.username;
        """
        if "users" in tables:
            merge_sql += """
            INSERT INTO _users_unified
                (username, password_hash, email, nama, role,
                 admin_id, invite_code, created_at, updated_at)
            SELECT us.username, us.password_hash, us.email, us.nama, 'user',
                   (SELECT new_id FROM _admin_map WHERE old_id = us.admin_id),
                   NULL, us.created_at, us.updated_at
            FROM users us;
            """
        if remap_results:
            merge_sql += """
            UPDATE analysis_results
               SET admin_id = (SELECT new_id FROM _admin_map
                               WHERE old_id = analysis_results.admin_id)
             WHERE EXISTS (SELECT 1 FROM _admin_map
                           WHERE old_id = analysis_results.admin_id);
            """
        if "users" in tables:
            merge_sql += "DROP TABLE users;\n"
        merge_sql += """
            DROP TABLE admins;
            DROP TABLE _admin_map;
            ALTER TABLE _users_unified RENAME TO users;
        """
        conn.executescript(merge_sql)
        conn.commit()
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )}

    # ── Create tables (fresh install) ────────────────────────────────────────────
    # Tabel referensi (master) dipisah agar ternormalisasi & tidak hardcode:
    #   role  → daftar peran akun (admin/user), dirujuk users.role_id
    #   kelas → daftar kelas kesesuaian (S1/S2/S3/N), dirujuk analysis_results.kelas_id
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS role (
            id   INTEGER PRIMARY KEY AUTOINCREMENT,
            nama TEXT    NOT NULL UNIQUE
        );

        CREATE TABLE IF NOT EXISTS kelas (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            kode          TEXT    NOT NULL UNIQUE,
            nama          TEXT    NOT NULL,
            deskripsi     TEXT,
            rentang       TEXT,
            potensi       TEXT,
            karakteristik TEXT,
            rekomendasi   TEXT
        );

        CREATE TABLE IF NOT EXISTS users (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            username      TEXT    NOT NULL UNIQUE,
            password_hash TEXT    NOT NULL,
            email         TEXT,
            nama          TEXT,
            role_id       INTEGER NOT NULL DEFAULT 2 REFERENCES role(id),
            admin_id      INTEGER REFERENCES users(id),
            invite_code   TEXT    UNIQUE,
            created_at    TIMESTAMP DEFAULT (datetime('now')),
            updated_at    TIMESTAMP DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS analysis_results (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            name            TEXT    NOT NULL,
            description     TEXT,
            polygon_geojson TEXT    NOT NULL,
            raw_params      TEXT    NOT NULL,
            saw_result      TEXT    NOT NULL,
            kelas_id        INTEGER REFERENCES kelas(id),
            total_score     REAL    NOT NULL,
            admin_id        INTEGER NOT NULL REFERENCES users(id),
            created_at      TIMESTAMP DEFAULT (datetime('now')),
            updated_at      TIMESTAMP DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS analysis_logs (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            result_id   INTEGER NOT NULL REFERENCES analysis_results(id),
            run_number  INTEGER NOT NULL DEFAULT 1,
            logged_at   TEXT    NOT NULL,
            level       TEXT    NOT NULL,
            message     TEXT    NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_analysis_logs_result
            ON analysis_logs(result_id, run_number);

        -- Permintaan analisis: user menggambar plot lahan, admin yang menganalisis.
        --   admin_id  → admin (tenant) tujuan permintaan
        --   user_id   → user pengaju
        --   status    → pending | approved | rejected
        --   result_id → diisi saat admin selesai menganalisis (FK ke hasil)
        CREATE TABLE IF NOT EXISTS analysis_requests (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            name            TEXT    NOT NULL,
            description     TEXT,
            polygon_geojson TEXT    NOT NULL,
            status          TEXT    NOT NULL DEFAULT 'pending',
            admin_note      TEXT,
            admin_id        INTEGER NOT NULL REFERENCES users(id),
            user_id         INTEGER NOT NULL REFERENCES users(id),
            result_id       INTEGER REFERENCES analysis_results(id),
            created_at      TIMESTAMP DEFAULT (datetime('now')),
            updated_at      TIMESTAMP DEFAULT (datetime('now'))
        );

        CREATE INDEX IF NOT EXISTS idx_analysis_requests_admin
            ON analysis_requests(admin_id, status);
        CREATE INDEX IF NOT EXISTS idx_analysis_requests_user
            ON analysis_requests(user_id);
    """)

    # ── Seed tabel referensi role & kelas (master data, idempotent) ──────────────
    # Id ditetapkan eksplisit agar stabil & dapat dijadikan default FK.
    conn.executemany(
        "INSERT OR IGNORE INTO role (id, nama) VALUES (?, ?)",
        [(1, "admin"), (2, "user")],
    )
    # Tambah kolom deskriptif pada tabel kelas yang dibuat versi awal (id/kode/nama/deskripsi).
    kelas_cols = {r[1] for r in conn.execute("PRAGMA table_info(kelas)")}
    for col in ("rentang", "potensi", "karakteristik", "rekomendasi"):
        if col not in kelas_cols:
            conn.execute(f"ALTER TABLE kelas ADD COLUMN {col} TEXT")

    # Konten kelas dijadikan satu sumber kebenaran di DB (bukan hardcode di template/PDF).
    # UPSERT: baris dibuat bila belum ada, atau teksnya disegarkan agar selalu sinkron.
    conn.executemany(
        """INSERT INTO kelas
               (id, kode, nama, deskripsi, rentang, potensi, karakteristik, rekomendasi)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(kode) DO UPDATE SET
               nama=excluded.nama, deskripsi=excluded.deskripsi, rentang=excluded.rentang,
               potensi=excluded.potensi, karakteristik=excluded.karakteristik,
               rekomendasi=excluded.rekomendasi""",
        [
            (1, "S1", "Sangat Sesuai",
             "Lahan tidak memiliki faktor pembatas yang berarti atau nyata terhadap penggunaan secara berkelanjutan, atau hanya memiliki faktor pembatas yang bersifat tidak dominan dan tidak akan mereduksi produktivitas lahan secara nyata.",
             "0.8125 – 1.0000",
             "Lahan memiliki potensi sangat tinggi untuk budidaya padi lahan basah. Seluruh atau hampir seluruh parameter fisik dan lingkungan (seperti regime hidrologi, drainase, tekstur tanah, dan retensi hara) berada dalam kondisi optimal yang mendukung pertumbuhan dan produktivitas tanaman secara maksimal.",
             "Lahan tidak memiliki faktor pembatas yang berarti, atau hanya memiliki pembatas minor yang bersifat tidak nyata dan tidak berpengaruh terhadap penurunan produktivitas tanaman secara berkelanjutan.",
             "Lahan dapat langsung dimanfaatkan untuk budidaya padi lahan basah. Disarankan menerapkan manajemen pengairan yang teratur serta pemupukan berimbang spesifik lokasi guna mempertahankan kestabilan dan memaksimalkan hasil panen."),
            (2, "S2", "Cukup Sesuai",
             "Lahan mempunyai faktor pembatas yang akan berpengaruh terhadap produktivitasnya sehingga memerlukan tambahan masukan (input). Faktor pembatas tersebut biasanya masih dapat diatasi oleh petani sendiri.",
             "0.6250 – 0.8125",
             "Lahan memiliki potensi sedang untuk budidaya padi lahan basah. Produktivitas lahan masih cukup optimal, namun sedikit terhambat oleh beberapa faktor pembatas lingkungan atau karakteristik tanah.",
             "Lahan memiliki faktor pembatas moderat (misalnya: fluktuasi ketersediaan air musiman, kemiringan lereng landai, atau defisiensi unsur hara makro tertentu). Pembatas ini dapat menurunkan produktivitas atau meningkatkan biaya investasi pengelolaan.",
             "Lahan dapat digunakan untuk budidaya padi lahan basah dengan syarat menerapkan tindakan perbaikan (input) tingkat sedang. Direkomendasikan melakukan penataan saluran irigasi/drainase, penambahan bahan organik secara intensif, atau pengapuran untuk menetralisir faktor pembatas tersebut."),
            (3, "S3", "Sesuai Marginal",
             "Lahan mempunyai faktor pembatas yang dominan dan berpengaruh terhadap produktivitasnya, memerlukan tambahan masukan yang lebih banyak daripada lahan kelas S2. Untuk mengatasi faktor pembatas ini diperlukan modal tinggi, sehingga perlu adanya bantuan kepada petani untuk mengatasinya.",
             "0.4375 – 0.6250",
             "Lahan memiliki potensi yang rendah untuk budidaya padi lahan basah. Jika dipaksakan tanpa pengelolaan khusus, produktivitas tanaman akan berada di bawah rata-rata dan kurang menguntungkan secara ekonomi.",
             "Lahan memiliki faktor pembatas yang berat dan bersifat akumulatif (misalnya: tekstur tanah terlalu pasiran sehingga sulit menahan air, risiko banjir musiman yang ekstrem/lama, salinitas tinggi, atau lapisan olah tanah yang dangkal). Pembatas ini sangat mengganggu pertumbuhan vegetatif dan generatif tanaman.",
             "Memerlukan modal, teknologi, dan pengelolaan lahan tingkat tinggi sebelum atau selama budidaya dilakukan. Rekomendasi meliputi pembuatan tanggul penahan banjir/infrastruktur tata air (pintu air), pemberian amelioran (seperti biochar, kapur, atau pupuk kandang) dosis tinggi, serta penggunaan varietas padi lahan basah yang toleran terhadap cekaman lingkungan spesifik."),
            (4, "N", "Tidak Sesuai",
             "Lahan tidak sesuai untuk diusahakan karena mempunyai faktor pembatas yang sangat dominan dan/atau sulit diatasi.",
             "0.2500 – 0.4375",
             "Lahan tidak potensial dan tidak direkomendasikan untuk budidaya padi lahan basah karena kendala fisik, kimia, atau lingkungan yang sangat ekstrem.",
             "Lahan memiliki pembatas yang sangat berat atau bersifat permanen yang tidak mungkin diatasi dengan tingkat teknologi dan biaya rasional saat ini (misalnya: topografi berbukit terjal, singkapan batuan yang masif, kedalaman sulfat masam yang sangat dangkal dan beracun, atau status lahan merupakan kawasan konservasi).",
             "Sangat tidak disarankan untuk budidaya padi lahan basah. Penggunaan lahan sebaiknya dialihkan untuk fungsi konservasi, hutan kemasyarakatan, atau agroforestri/tanaman tahunan yang sesuai dengan karakteristik agroekosistem setempat demi mencegah kerusakan lingkungan dan degradasi lahan."),
        ],
    )

    # ── Forward migrations for users (add columns missing on older unified DBs) ──
    existing_users = {r[1] for r in conn.execute("PRAGMA table_info(users)")}
    for col, typedef in [
        ("email",       "TEXT"),
        ("nama",        "TEXT"),
        ("admin_id",    "INTEGER REFERENCES users(id)"),
        ("invite_code", "TEXT"),
        ("updated_at",  "TIMESTAMP DEFAULT (datetime('now'))"),
    ]:
        if col not in existing_users:
            conn.execute(f"ALTER TABLE users ADD COLUMN {col} {typedef}")

    # ── Migrasi role (TEXT) → role_id (FK ke tabel role) ─────────────────────────
    existing_users = {r[1] for r in conn.execute("PRAGMA table_info(users)")}
    if "role_id" not in existing_users:
        conn.execute("ALTER TABLE users ADD COLUMN role_id INTEGER REFERENCES role(id)")
    if "role" in existing_users:
        # Petakan nilai teks lama ('admin'/'user') ke id pada tabel role.
        conn.execute(
            "UPDATE users SET role_id = (SELECT id FROM role WHERE role.nama = users.role)"
            " WHERE role_id IS NULL"
        )
    # Baris tanpa peran dianggap user biasa (id=2).
    conn.execute("UPDATE users SET role_id = 2 WHERE role_id IS NULL")
    # Buang kolom teks lama agar skema benar-benar ternormalisasi.
    if "role" in {r[1] for r in conn.execute("PRAGMA table_info(users)")}:
        conn.execute("ALTER TABLE users DROP COLUMN role")

    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_users_invite_code ON users(invite_code)"
    )

    # ── Forward migrations for analysis_results ───────────────────────────────────
    existing_ar = {r[1] for r in conn.execute("PRAGMA table_info(analysis_results)")}
    if "admin_id" not in existing_ar:
        conn.execute(
            "ALTER TABLE analysis_results ADD COLUMN admin_id INTEGER REFERENCES users(id)"
        )
        if "created_by" in existing_ar:
            conn.execute(
                "UPDATE analysis_results SET admin_id = created_by WHERE admin_id IS NULL"
            )

    # ── Migrasi kelas (TEXT) → kelas_id (FK ke tabel kelas) ──────────────────────
    existing_ar = {r[1] for r in conn.execute("PRAGMA table_info(analysis_results)")}
    if "kelas_id" not in existing_ar:
        conn.execute(
            "ALTER TABLE analysis_results ADD COLUMN kelas_id INTEGER REFERENCES kelas(id)"
        )
    if "kelas" in existing_ar:
        # Petakan kode lama ('S1'..'N') ke id pada tabel kelas.
        conn.execute(
            "UPDATE analysis_results SET kelas_id = "
            "(SELECT id FROM kelas WHERE kelas.kode = analysis_results.kelas)"
            " WHERE kelas_id IS NULL"
        )
        conn.execute("ALTER TABLE analysis_results DROP COLUMN kelas")

    # ── Seed default admin (kredensial diambil dari .env) ─────────────────────────
    seed_username = os.getenv("DEFAULT_ADMIN_USERNAME", "admin")
    seed_password = os.getenv("DEFAULT_ADMIN_PASSWORD")
    seed_email    = os.getenv("DEFAULT_ADMIN_EMAIL", "admin@example.com")
    if conn.execute(
        "SELECT id FROM users WHERE username=?", (seed_username,)
    ).fetchone() is None:
        if not seed_password:
            conn.close()
            raise RuntimeError(
                "DEFAULT_ADMIN_PASSWORD wajib diatur untuk membuat akun admin awal."
            )
        conn.execute(
            "INSERT INTO users (username, password_hash, email, role_id, invite_code)"
            " VALUES (?,?,?,(SELECT id FROM role WHERE nama='admin'),?)",
            (seed_username, generate_password_hash(seed_password), seed_email,
             _unique_invite_code(conn)),
        )

    # ── Backfill invite_code for admins that don't have one ───────────────────────
    for row in conn.execute(
        "SELECT id FROM users WHERE role_id=(SELECT id FROM role WHERE nama='admin')"
        " AND invite_code IS NULL"
    ).fetchall():
        conn.execute(
            "UPDATE users SET invite_code=? WHERE id=?",
            (_unique_invite_code(conn), row[0]),
        )

    conn.commit()
    conn.close()
