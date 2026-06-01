# Copyright (c) 2021, Frappe and contributors
# For license information, please see license.txt

import random

import frappe
from frappe import _
from frappe.desk.doctype.notification_log.notification_log import make_notification_logs
from frappe.model.document import Document
from frappe.utils import cint, today

from ...utils import (
	generate_slug,
	get_instructors,
	get_lms_route,
	update_payment_record,
	validate_image,
)


class LMSCourse(Document):
	def validate(self):
		self.validate_not_deleted_for_edit()
		self.validate_published()
		self.validate_instructors()
		self.validate_video_link()
		self.validate_status()
		self.validate_payments_app()
		self.validate_certification()
		self.validate_amount_and_currency()
		self.image = validate_image(self.image)
		self.validate_card_gradient()

	def validate_not_deleted_for_edit(self):
		"""Refuse user-initiated edits to a soft-deleted course.

		Flipping the trash flags themselves (entering or leaving Trash) is
		always allowed, otherwise restore would be blocked by its own guard.
		`frappe.db.set_value` bypasses `validate` entirely, so internal
		scheduled writes (e.g. update_course_statistics counters) still work;
		each such call site has its own explicit `is_deleted` gate.
		"""
		if not self.is_deleted:
			return
		if self.has_value_changed("is_deleted"):
			return  # entering or leaving Trash
		# If only the trash audit fields changed, allow.
		changed = {
			f
			for f in ("is_deleted", "deleted_on", "deleted_by", "published")
			if self.has_value_changed(f)
		}
		non_trash_changes = any(
			self.has_value_changed(f.fieldname)
			for f in self.meta.get("fields")
			if f.fieldname not in changed
		)
		if non_trash_changes:
			frappe.throw(
				_(
					"This course is in Trash and cannot be edited. Restore it from the Trash view first."
				),
				title=_("Course in Trash"),
			)

	def validate_published(self):
		if self.published and not self.published_on:
			self.published_on = today()

	def validate_instructors(self):
		if self.is_new() and not self.instructors:
			frappe.get_doc(
				{
					"doctype": "Course Instructor",
					"instructor": self.owner,
					"parent": self.name,
					"parentfield": "instructors",
					"parenttype": "LMS Course",
				}
			).save(ignore_permissions=True)

	def validate_video_link(self):
		if self.video_link and "/" in self.video_link:
			self.video_link = self.video_link.split("/")[-1]

	def validate_status(self):
		if self.published:
			self.status = "Approved"

	def validate_payments_app(self):
		if self.paid_course:
			installed_apps = frappe.get_installed_apps()
			if "payments" not in installed_apps:
				documentation_link = "https://docs.frappe.io/learning/setting-up-payment-gateway"
				frappe.throw(
					_(
						"Please install the Payments App to create a paid course. Refer to the documentation for more details. {0}"
					).format(documentation_link)
				)

	def validate_certification(self):
		if self.enable_certification and self.paid_certificate:
			frappe.throw(_("A course cannot have both paid certificate and certificate of completion."))

		if self.paid_certificate and not self.evaluator:
			frappe.throw(_("Evaluator is required for paid certificates."))

		if self.paid_certificate and not self.timezone:
			frappe.throw(_("Timezone is required for paid certificates."))

	def validate_amount_and_currency(self):
		if self.paid_course and (cint(self.course_price) < 0 or not self.currency):
			frappe.throw(_("Amount and currency are required for paid courses."))

		if self.paid_certificate and (cint(self.course_price) <= 0 or not self.currency):
			frappe.throw(_("Amount and currency are required for paid certificates."))

	def validate_card_gradient(self):
		if not self.image and not self.card_gradient:
			colors = [
				"Red",
				"Blue",
				"Green",
				"Yellow",
				"Orange",
				"Pink",
				"Amber",
				"Violet",
				"Cyan",
				"Teal",
				"Gray",
				"Purple",
			]
			self.card_gradient = random.choice(colors)

	def on_update(self):
		if self.is_deleted:
			# Trashed course must never email interested users about availability.
			return
		if not self.upcoming and self.has_value_changed("upcoming"):
			self.send_email_to_interested_users()

	def on_payment_authorized(self, payment_status):
		if payment_status in ["Authorized", "Completed"]:
			update_payment_record("LMS Course", self.name)

	def send_email_to_interested_users(self):
		interested_users = frappe.get_all("LMS Course Interest", {"course": self.name}, ["name", "user"])
		subject = self.title + " is available!"
		args = {
			"title": self.title,
			"course_link": get_lms_route(f"courses/{self.name}"),
			"app_name": frappe.db.get_single_value("System Settings", "app_name"),
			"site_url": frappe.utils.get_url(),
		}

		for user in interested_users:
			args["first_name"] = frappe.db.get_value("User", user.user, "first_name")
			email_args = frappe._dict(
				recipients=user.user,
				subject=subject,
				header=[subject, "green"],
				template="lms_course_interest",
				args=args,
				now=True,
			)
			frappe.enqueue(method=frappe.sendmail, queue="short", timeout=300, is_async=True, **email_args)
			frappe.db.set_value("LMS Course Interest", user.name, "email_sent", True)

	def autoname(self):
		if not self.name:
			self.name = generate_slug(self.title, "LMS Course")

	def __repr__(self):
		return f"<Course#{self.name}>"


def send_notification_for_published_courses():
	send_notification = frappe.db.get_single_value("LMS Settings", "send_notification_for_published_courses")
	if not send_notification:
		return

	courses_published_today = frappe.get_all(
		"LMS Course",
		{
			"published_on": today(),
			"notification_sent": 0,
		},
		["name", "title", "short_introduction"],
	)

	if not courses_published_today:
		return

	if send_notification == "Email":
		send_email_notification_for_published_courses(courses_published_today)
	else:
		send_system_notification_for_published_courses(courses_published_today)


def send_email_notification_for_published_courses(courses):
	brand_name = frappe.db.get_single_value("Website Settings", "app_name")
	brand_logo = frappe.db.get_single_value("Website Settings", "banner_image")
	subject = _("A new course has been published on {0}").format(brand_name)
	template = "published_course_notification"
	students = frappe.get_all("User", {"enabled": 1}, pluck="name")

	for course in courses:
		instructors = get_instructors("LMS Course", course.name)

		args = {
			"brand_logo": brand_logo,
			"brand_name": brand_name,
			"title": course.title,
			"short_introduction": course.short_introduction,
			"instructors": instructors,
			"course_url": frappe.utils.get_url(get_lms_route(f"courses/{course.name}")),
		}

		frappe.sendmail(
			recipients=instructors,
			bcc=students,
			subject=subject,
			template=template,
			args=args,
		)
		frappe.db.set_value("LMS Course", course.name, "notification_sent", 1)


def send_system_notification_for_published_courses(courses):
	for course in courses:
		students = frappe.get_all("User", {"enabled": 1}, pluck="name")
		instructors = frappe.get_all("Course Instructor", {"parent": course.name}, pluck="instructor")
		instructor_name = frappe.db.get_value("User", instructors[0], "full_name")
		notification = frappe._dict(
			{
				"subject": _("{0} has published a new course {1}").format(
					frappe.bold(instructor_name), frappe.bold(course.title)
				),
				"email_content": _(
					"A new course '{0}' has been published that might interest you. Check it out!"
				).format(course.title),
				"document_type": "LMS Course",
				"document_name": course.name,
				"from_user": instructors[0] if instructors else None,
				"type": "Alert",
				"link": get_lms_route(f"courses/{course.name}"),
			}
		)
		make_notification_logs(notification, students)
		frappe.db.set_value("LMS Course", course.name, "notification_sent", 1)


def get_permission_query_conditions(user):
	"""Hide soft-deleted LMS Course rows from list queries.

	Frappe applies this hook only to `frappe.get_all` / `frappe.get_list` and
	to Desk autocompletes via `search_link`. It does NOT cover `frappe.db.*`
	low-level calls (get_value, exists, count, qb, raw SQL) — those need
	explicit `is_deleted` filters at the call site.

	Bypasses:
	- `frappe.flags.lms_show_trash` is set by the Trash UI endpoint so admins
	  can see trashed courses.
	- During migrate / patches / install we never filter — pending patches
	  may need to iterate every row including trashed ones.

	Defensive: if the `is_deleted` column doesn't exist yet (migration window
	on a fresh deploy before `bench migrate` has run), no filter — old data
	is fine, the schema add is backward-compatible.
	"""
	if frappe.flags.in_migrate or frappe.flags.in_patch or frappe.flags.in_install:
		return None
	if getattr(frappe.flags, "lms_show_trash", False):
		return None
	if not frappe.db.has_column("LMS Course", "is_deleted"):
		return None
	return "`tabLMS Course`.is_deleted = 0"
