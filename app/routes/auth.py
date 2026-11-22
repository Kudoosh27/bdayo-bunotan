from flask import Blueprint, flash, g, redirect, render_template, request, session, url_for

from ..models import User, db
from ..auth_state import AnonymousUser, current_user
from ..services.audit import record_audit
from ..services.passwords import hash_password, verify_password
from ..services.access import roles_required
from ..services.rate_limits import rate_limit

bp = Blueprint("auth", __name__)
_DUMMY_PASSWORD_HASH = hash_password("not-a-real-account-password")


@bp.route("/login", methods=["GET", "POST"])
@rate_limit("login", 5, 60, 300)
def login():
    if request.method == "POST":
        raw_username = request.form.get("username", "").strip()
        username = raw_username[:120]
        raw_password = request.form.get("password", "")
        password = raw_password[:1024]
        user = User.query.filter_by(username=username).first()
        password_valid = (
            user.check_password(password)
            if user
            else verify_password(_DUMMY_PASSWORD_HASH, password)
        )
        valid = (
            user is not None
            and user.active
            and len(raw_username) <= 120
            and len(raw_password) <= 1024
            and password_valid
        )
        if valid:
            session.clear()
            session["user_id"] = str(user.id)
            g.current_user = user
            record_audit("login_succeeded", "user", user.id)
            db.session.commit()
            return redirect(url_for("admin.dashboard" if user.role == "admin" else "leader.dashboard"))
        record_audit("login_failed", "user")
        db.session.commit()
        flash("The username or password is incorrect.", "danger")
    return render_template("auth/login.html")


@bp.post("/logout")
@roles_required("admin", "leader")
def logout():
    record_audit("logout", "user", current_user.id)
    db.session.commit()
    session.clear()
    g.current_user = AnonymousUser()
    flash("You have been signed out.", "info")
    return redirect(url_for("public.home"))
