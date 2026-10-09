"""Flask-Login User model and login manager."""
from flask_login import LoginManager, UserMixin

from database import get_db

login_manager = LoginManager()
login_manager.login_view = "login"
login_manager.login_message = "Silakan login terlebih dahulu."
login_manager.login_message_category = "warning"


class User(UserMixin):
    def __init__(self, id: int, username: str, role: str, admin_id=None, invite_code=None):
        self.id = id
        self.username = username
        self.role = role
        self.admin_id = admin_id    # NULL for admin accounts; set for user accounts
        self.invite_code = invite_code  # Only set for admin accounts

    def get_id(self):
        """Return the account's primary-key id as a string (Flask-Login requirement)."""
        return str(self.id)

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"

    @property
    def tenant_id(self) -> int:
        """The admin (tenant) ID that scopes this user's data.
        For admin accounts returns their own id; for user accounts returns their admin's id."""
        return self.id if self.is_admin else self.admin_id

    @property
    def needs_invite(self) -> bool:
        """True if user hasn't joined any admin's tenant yet."""
        return not self.is_admin and self.admin_id is None


@login_manager.user_loader
def load_user(user_id):
    # Accept both the new plain-id format and the legacy "role:id" format so
    # existing sessions don't break right after the schema merge.
    raw = user_id.rsplit(":", 1)[-1] if isinstance(user_id, str) else user_id
    try:
        uid = int(raw)
    except (ValueError, TypeError):
        return None

    conn = get_db()
    row = conn.execute(
        "SELECT u.id, u.username, r.nama AS role, u.admin_id, u.invite_code"
        " FROM users u JOIN role r ON u.role_id = r.id WHERE u.id=?", (uid,)
    ).fetchone()
    conn.close()
    if row:
        return User(row["id"], row["username"], row["role"],
                    row["admin_id"], row["invite_code"])
    return None
