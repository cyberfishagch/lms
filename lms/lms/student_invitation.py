from datetime import timedelta
from urllib.parse import parse_qs, urlparse

import frappe
from frappe.utils import cint, now_datetime, sha256_hash
from frappe.utils.password import Auth

LMS_ADMIN_ROLES = ("Moderator", "Course Creator", "Batch Evaluator")
REDACTED_SETUP_EMAIL = "[PASSWORD SETUP EMAIL BODY REDACTED]"
STUDENT_INVITATION_LOCK_TIMEOUT = 300


def _user_has_password(user: str) -> bool:
	return bool(
		frappe.qb.from_(Auth)
		.select(Auth.name)
		.where(
			(Auth.doctype == "User")
			& (Auth.name == user)
			& (Auth.fieldname == "password")
			& (Auth.encrypted == 0)
		)
		.limit(1)
		.run()
	)


def _has_active_reset_key(user_doc) -> bool:
	if not user_doc.reset_password_key:
		return False

	expiry_seconds = cint(
		frappe.db.get_single_value("System Settings", "reset_password_link_expiry_duration")
	)
	generated_on = user_doc.last_reset_password_key_generated_on
	return bool(
		not expiry_seconds
		or not generated_on
		or now_datetime() <= generated_on + timedelta(seconds=expiry_seconds)
	)


def student_invitation_lock(user: str):
	"""Serialize assignment email state across EmailQueue's internal commits."""
	return frappe.cache.lock(
		f"{frappe.conf.db_name}:lms:student-invitation:{sha256_hash(user)}",
		timeout=STUDENT_INVITATION_LOCK_TIMEOUT,
		blocking_timeout=STUDENT_INVITATION_LOCK_TIMEOUT,
	)


def get_student_password_setup_url(user: str) -> str | None:
	"""Return a setup URL for an enabled, student-only deferred account."""
	if not frappe.db.exists(
		"Has Role",
		{"parent": user, "parenttype": "User", "role": "LMS Student"},
	):
		return None
	if frappe.db.exists(
		"Has Role",
		{"parent": user, "parenttype": "User", "role": ["in", LMS_ADMIN_ROLES]},
	):
		return None

	# The lock serializes key generation until EmailQueue.send commits. After that
	# commit, concurrent assignments see the active key and do not rotate it.
	user_doc = frappe.get_doc("User", user, for_update=True)
	if user_doc.name == "Administrator" or not user_doc.enabled or cint(user_doc.send_welcome_email):
		return None

	# send_welcome_email predates this flow. Do not treat legacy students who
	# already have credentials as newly deferred accounts.
	if _user_has_password(user):
		return None
	if _has_active_reset_key(user_doc):
		return None

	return user_doc._reset_password()


def ensure_password_setup_email_sent(email_queue, password_setup_url: str | None):
	"""Raise when an immediate password-setup email was not sent."""
	if not password_setup_url:
		return

	status = frappe.db.get_value("Email Queue", email_queue.name, "status") if email_queue else None
	if status != "Sent":
		raise frappe.OutgoingEmailError("Password setup email was not sent.")


def append_student_password_setup(content: str, password_setup_url: str | None) -> str:
	"""Ensure custom assignment templates include deferred password setup."""
	if not password_setup_url or password_setup_url in content:
		return content

	setup_content = frappe.render_template(
		"lms/templates/emails/student_password_setup.html",
		{"password_setup_url": password_setup_url},
	)
	return f"{content}{setup_content}"


def redact_student_password_setup(email_queue):
	"""Remove the bearer setup link from a successfully sent queue record."""
	message = email_queue.message or ""
	separator = "\r\n\r\n" if "\r\n\r\n" in message else "\n\n"
	headers, found, _body = message.partition(separator)
	redacted_message = f"{headers}{found}{REDACTED_SETUP_EMAIL}" if found else REDACTED_SETUP_EMAIL
	frappe.db.set_value(
		"Email Queue",
		email_queue.name,
		"message",
		redacted_message,
		update_modified=False,
	)


def mark_student_welcome_sent(user: str):
	"""Mark the deferred student welcome as delivered with an assignment email."""
	frappe.db.set_value("User", user, "send_welcome_email", 1)


def complete_student_password_setup(user: str, email_queue, password_setup_url: str | None) -> bool:
	"""Persist successful setup delivery and remove its bearer link from the queue."""
	ensure_password_setup_email_sent(email_queue, password_setup_url)
	if not password_setup_url:
		return False

	redact_student_password_setup(email_queue)
	mark_student_welcome_sent(user)
	return True


def abandon_student_password_setup(user: str, email_queue, password_setup_url: str):
	"""Invalidate this setup link and prevent its failed queue from being retried."""
	key = parse_qs(urlparse(password_setup_url).query).get("key", [None])[0]
	if key:
		frappe.db.set_value(
			"User",
			{"name": user, "reset_password_key": sha256_hash(key)},
			{
				"reset_password_key": "",
				"last_reset_password_key_generated_on": None,
			},
		)

	if email_queue:
		redact_student_password_setup(email_queue)
		frappe.db.set_value(
			"Email Queue",
			email_queue.name,
			{
				"status": "Error",
				"error": "Immediate password setup delivery failed.",
			},
		)

	frappe.db.commit()


def send_student_password_setup_email(user: str, password_setup_url: str, **email_args) -> bool:
	"""Send the first credential-bearing assignment email synchronously."""
	email_queue = None
	try:
		email_queue = frappe.sendmail(now=False, **email_args)
		if not email_queue:
			raise frappe.OutgoingEmailError("Password setup email could not be queued.")

		email_queue.send()
		return complete_student_password_setup(user, email_queue, password_setup_url)
	except Exception:
		abandon_student_password_setup(user, email_queue, password_setup_url)
		raise
