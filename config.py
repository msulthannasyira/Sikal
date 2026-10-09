import os
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))


class Config:
    SECRET_KEY = os.getenv("FLASK_SECRET_KEY")
    DATABASE = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "instance", "app.db"
    )
    # Project & service account dibaca otomatis dari isi file kunci JSON
    # (lihat gee_analysis._init_gee), jadi cukup satu variabel path ini.
    GEE_PRIVATE_KEY_JSON = os.getenv("GEE_PRIVATE_KEY_JSON", "")
    HCAPTCHA_SITE_KEY = os.getenv("HCAPTCHA_SITE_KEY", "")
    HCAPTCHA_SECRET_KEY = os.getenv("HCAPTCHA_SECRET_KEY", "")
    HCAPTCHA_ENABLED = os.getenv("HCAPTCHA_ENABLED", "true").lower() == "true"
