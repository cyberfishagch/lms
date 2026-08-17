import json
from datetime import timedelta

import frappe
from frappe import _
from frappe.model.naming import append_number_if_name_exists
from frappe.rate_limiter import rate_limit
from frappe.utils import (
	cint,
	escape_html,
	get_fullname,
	get_url,
	now_datetime,
	random_string,
	validate_email_address,
)
from frappe.utils.data import sha256_hash
from frappe.utils.password import check_password, get_password_reset_limit
from frappe.website.utils import cleanup_page_name, is_signup_disabled

from lms.lms.utils import get_country_code, get_lms_route


def validate_username_duplicates(doc, method):
	while not doc.username or doc.username_exists():
		doc.username = append_number_if_name_exists(
			doc.doctype, cleanup_page_name(doc.full_name), fieldname="username"
		)
	if " " in doc.username:
		doc.username = doc.username.replace(" ", "")

	if len(doc.username) < 4:
		doc.username = doc.email.replace("@", "").replace(".", "")


def after_insert(doc, method):
	doc.add_roles("LMS Student")


@frappe.whitelist(allow_guest=True)
def sign_up(email: str, full_name: str, verify_terms: bool, user_category: str):
	if is_signup_disabled():
		frappe.throw(_("Sign Up is disabled"), _("Not Allowed"))

	user = frappe.db.get("User", {"email": email})
	if user:
		if user.enabled:
			return 0, _("Already Registered")
		else:
			return 0, _("Registered but disabled")
	else:
		if frappe.db.get_creation_count("User", 60) > 300:
			frappe.respond_as_web_page(
				_("Temporarily Disabled"),
				_(
					"Too many users signed up recently, so the registration is disabled. Please try back in an hour"
				),
				http_status_code=429,
			)

	user = frappe.get_doc(
		{
			"doctype": "User",
			"email": email,
			"first_name": escape_html(full_name),
			"verify_terms": verify_terms,
			"user_category": user_category,
			"country": "",
			"enabled": 1,
			"new_password": random_string(10),
			"user_type": "Website User",
		}
	)
	user.flags.ignore_permissions = True
	user.flags.ignore_password_policy = True
	user.insert()

	# set default signup role as per Portal Settings
	default_role = frappe.db.get_single_value("Portal Settings", "default_role")
	if default_role:
		user.add_roles(default_role)

	user.add_roles("LMS Student")
	set_country_from_ip(None, user.name)

	if user.flags.email_sent:
		return 1, _("Please check your email for verification")
	else:
		return 2, _("Please ask your administrator to verify your sign-up")


def set_country_from_ip(login_manager: object = None, user: str = None):
	if not user and login_manager:
		user = login_manager.user
	user_country = frappe.db.get_value("User", user, "country")
	if user_country:
		return
	frappe.db.set_value("User", user, "country", get_country_code())
	return


def on_login(login_manager):
	default_app = frappe.db.get_single_value("System Settings", "default_app")
	if default_app == "lms":
		frappe.local.response["home_page"] = get_lms_route()


@frappe.whitelist(allow_guest=True, methods=["POST"])
@rate_limit(limit=get_password_reset_limit, seconds=60 * 60)
def resend_expired_password_link(key: str):
	"""Send a fresh password-reset email when ``key`` is genuinely expired."""
	_resend_expired_password_link((key or "").strip())
	return {"message": _("A new password reset email has been sent.")}


def _resend_expired_password_link(key: str):
	if not key:
		return

	hashed_key = sha256_hash(key)
	user = frappe.db.get_value("User", {"reset_password_key": hashed_key}, "name")
	if not user:
		return

	user_doc = frappe.get_doc("User", user, for_update=True)
	if user_doc.reset_password_key != hashed_key:
		return

	expiry_seconds = cint(
		frappe.db.get_single_value("System Settings", "reset_password_link_expiry_duration")
	)
	generated_on = user_doc.last_reset_password_key_generated_on
	if (
		not expiry_seconds
		or not generated_on
		or now_datetime() <= generated_on + timedelta(seconds=expiry_seconds)
	):
		return

	if user_doc.name == "Administrator" or not user_doc.enabled:
		return

	user_doc.validate_reset_password()
	user_doc.reset_password(send_email=True)


# ---------------------------------------------------------------------------
# Self-service login-email change, verified by a confirmation link.
#
# In Frappe a ``User`` document's name *is* its email, so changing the login
# email is a ``rename_doc`` — not a field edit. The rename re-syncs the
# ``email`` field and clears every one of the user's sessions in
# ``User.after_rename``. A learner has no rename permission, so this two-step,
# server-mediated flow does it on their behalf:
#
#   1. ``request_email_change`` runs as the logged-in user, re-authenticates
#      with their current password, validates the new address is free and
#      well-formed, then mails a one-hour token to the **new** address.
#   2. ``confirm_email_change`` is opened from that emailed link (possibly in a
#      fresh browser, hence ``allow_guest``). Possession of the token proves the
#      user controls the new mailbox, so the rename runs with
#      ``ignore_permissions``.
#
# The pending change lives only in Redis (token -> {user, new_email}) with a
# TTL, so an abandoned request simply expires and nothing is written until
# confirmed.
# ---------------------------------------------------------------------------

# Redis namespace + lifetime for a pending email-change token.
EMAIL_CHANGE_TOKEN_PREFIX = "lms_email_change"
EMAIL_CHANGE_TOKEN_TTL_SECONDS = 60 * 60  # 1 hour


def _email_change_cache_key(token: str) -> str:
	return f"{EMAIL_CHANGE_TOKEN_PREFIX}:{token}"


@frappe.whitelist(methods=["POST"])
@rate_limit(limit=10, seconds=60 * 60)
def request_email_change(new_email: str, current_password: str):
	"""Start a login-email change for the logged-in user.

	Re-authenticates with ``current_password``, validates ``new_email`` is a
	free, well-formed address, then mails a confirmation link to it. Returns the
	destination so the UI can tell the user where to look.
	"""
	user = frappe.session.user
	if not user or user == "Guest":
		frappe.throw(_("You must be signed in to change your email."), frappe.PermissionError)

	new_email = (new_email or "").strip().lower()
	if not new_email:
		frappe.throw(_("Enter a new email address."))

	# Raises on a malformed address.
	validate_email_address(new_email, throw=True)

	# Re-authenticate. ``check_password`` raises AuthenticationError on mismatch
	# or when the account has no password set (e.g. SSO-only users).
	try:
		check_password(user, current_password or "")
	except frappe.AuthenticationError:
		frappe.throw(_("Your current password is incorrect."), frappe.AuthenticationError)

	if new_email == user.lower():
		frappe.throw(_("That's already your email address."))

	if frappe.db.exists("User", new_email):
		frappe.throw(_("That email address is already in use."))

	token = frappe.generate_hash(length=48)
	frappe.cache().set_value(
		_email_change_cache_key(token),
		json.dumps({"user": user, "new_email": new_email}),
		expires_in_sec=EMAIL_CHANGE_TOKEN_TTL_SECONDS,
	)

	_send_email_change_confirmation(new_email, user, get_url(f"/confirm-email-change?token={token}"))

	return {"sent_to": new_email}


def _send_email_change_confirmation(new_email: str, user: str, link: str):
	full_name = get_fullname(user) or "there"
	frappe.sendmail(
		recipients=[new_email],
		subject=_("Confirm your new email address"),
		message=_(
			"<p>Hi {0},</p>"
			"<p>A request was made to change the login email on your Matchbox "
			"account to <strong>{1}</strong>.</p>"
			"<p>Confirm it with the button below. The link expires in one hour. "
			"If you didn't request this you can ignore this email — your account "
			"won't change.</p>"
			'<p style="margin:24px 0"><a href="{2}" '
			'style="background:#002B50;color:#fff;padding:10px 18px;'
			'border-radius:8px;text-decoration:none;display:inline-block">'
			"Confirm email change</a></p>"
			'<p style="color:#64748b;font-size:13px">Or paste this link into '
			"your browser:<br>{2}</p>"
		).format(full_name, new_email, link),
		now=True,
	)


@frappe.whitelist(allow_guest=True, methods=["POST"])
@rate_limit(limit=20, seconds=60 * 60)
def confirm_email_change(token: str):
	"""Finish a pending email change from the emailed confirmation link.

	Guest-allowed because the link may be opened in a browser with no session.
	Ownership of the new address is proven by possession of the token, which was
	mailed only to that address.
	"""
	token = (token or "").strip()
	raw = frappe.cache().get_value(_email_change_cache_key(token)) if token else None
	if not raw:
		frappe.throw(
			_("This confirmation link is invalid or has expired."),
			frappe.DoesNotExistError,
		)

	data = json.loads(raw)
	old_email = data["user"]
	new_email = data["new_email"]

	# Burn the token before renaming so a double-click can't replay.
	frappe.cache().delete_value(_email_change_cache_key(token))

	if not frappe.db.exists("User", old_email):
		frappe.throw(_("This account no longer exists."), frappe.DoesNotExistError)

	if frappe.db.exists("User", new_email):
		frappe.throw(_("That email address is now in use. Please try a different one."))

	# The rename IS the email change: User.after_rename re-syncs the `email`
	# field and clears all of the user's sessions.
	frappe.rename_doc("User", old_email, new_email, ignore_permissions=True)
	frappe.db.commit()

	return {"email": new_email}
