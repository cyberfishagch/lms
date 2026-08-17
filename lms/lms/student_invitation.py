import frappe
from frappe.utils import cint
from frappe.utils.password import Auth


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


def get_student_password_setup_url(user: str) -> str | None:
	"""Return a password setup URL for a student whose welcome email was deferred."""
	if not frappe.db.exists(
		"Has Role",
		{"parent": user, "parenttype": "User", "role": "LMS Student"},
	):
		return None

	# Serialize the first assignment so concurrent course and batch enrollments
	# cannot generate competing reset keys.
	user_doc = frappe.get_doc("User", user, for_update=True)
	if cint(user_doc.send_welcome_email):
		return None

	# send_welcome_email predates this flow. Do not treat legacy students who
	# already have credentials as newly deferred accounts.
	if _user_has_password(user):
		return None

	return user_doc.reset_password()


def append_student_password_setup(content: str, password_setup_url: str | None) -> str:
	"""Ensure custom assignment templates include deferred password setup."""
	if not password_setup_url or password_setup_url in content:
		return content

	setup_content = frappe.render_template(
		"lms/templates/emails/student_password_setup.html",
		{"password_setup_url": password_setup_url},
	)
	return f"{content}{setup_content}"


def mark_student_welcome_sent(user: str):
	"""Mark the deferred student welcome as delivered with an assignment email."""
	frappe.db.set_value("User", user, "send_welcome_email", 1)
