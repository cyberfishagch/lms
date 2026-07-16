# Copyright (c) 2021, FOSS United and contributors
# For license information, please see license.txt

import json

import frappe
from frappe import _
from frappe.email.doctype.email_template.email_template import get_email_template
from frappe.model.document import Document
from frappe.utils import ceil


class LMSEnrollment(Document):
	def before_insert(self):
		self.validate_duplicate_enrollment()
		self.validate_course_enrollment_eligibility()
		self.validate_owner()

	def after_insert(self):
		self.send_confirmation_email()

	def send_confirmation_email(self):
		"""Send a course enrollment confirmation email to the member.

		Mirrors the LMS Batch Enrollment confirmation flow: checks for an
		outgoing email account, sends once per enrollment, and skips emails
		for enrollments created automatically from a batch (the batch already
		sends its own confirmation).
		"""
		send_course_enrollment_email(self)

	def validate_owner(self):
		"""Makes the member as the owner of the document so that users can update their progress"""
		if self.owner != self.member:
			self.owner = self.member

	def on_update(self):
		update_program_progress(self.member)

	def validate_duplicate_enrollment(self):
		existing_enrollment = frappe.db.exists(
			"LMS Enrollment",
			{
				"course": self.course,
				"member": self.member,
				"name": ["!=", self.name],
			},
		)

		if existing_enrollment and existing_enrollment != self.name:
			frappe.throw(_("Student is already enrolled in this course."))

	def validate_course_enrollment_eligibility(self):
		course_details = frappe.db.get_value(
			"LMS Course",
			self.course,
			["published", "disable_self_learning", "paid_course", "paid_certificate", "is_deleted"],
			as_dict=True,
		)

		# Refuse enrollment to a soft-deleted course — for EVERYONE including
		# admins via bulk-enroll. Admins must restore the course first.
		if course_details.is_deleted:
			frappe.throw(_("This course has been deleted. Restore it from Trash before enrolling learners."))

		if course_details.disable_self_learning and not is_admin():
			frappe.throw(
				_(
					"You cannot enroll in this course as self-learning is disabled. Please contact the Administrator."
				)
			)

		if self.enrollment_from_batch:
			if frappe.db.exists(
				"LMS Batch Enrollment", {"batch": self.enrollment_from_batch, "member": self.member}
			):
				return

		if not course_details.published and not is_admin():
			frappe.throw(_("You cannot enroll in an unpublished course."))

		if course_details.paid_course:
			payment = frappe.db.exists(
				"LMS Payment",
				{
					"payment_for_document_type": "LMS Course",
					"payment_for_document": self.course,
					"member": self.member,
					"payment_received": True,
				},
			)

			if not payment:
				frappe.throw(_("You need to complete the payment for this course before enrolling."))


def is_admin():
	roles = frappe.get_roles(frappe.session.user)
	admin_roles = ["Moderator", "Course Creator", "Batch Evaluator"]
	for role in admin_roles:
		if role in roles:
			return True
	return False


def update_program_progress(member):
	programs = frappe.get_all("LMS Program Member", {"member": member}, ["parent", "name"])

	for program in programs:
		total_progress = 0
		courses = frappe.get_all("LMS Program Course", {"parent": program.parent}, pluck="course")
		for course in courses:
			progress = frappe.db.get_value("LMS Enrollment", {"course": course, "member": member}, "progress")
			progress = progress or 0
			total_progress += progress

		average_progress = ceil(total_progress / len(courses))
		frappe.db.set_value("LMS Program Member", program.name, "progress", average_progress)


def get_permission_query_conditions(user):
	"""Hide enrollments to soft-deleted courses from list queries.

	Critical companion to LMSCourse's hook: learner pages and many admin
	pages query `LMS Enrollment` directly (Dashboard, MyCourses, admin
	enrollment list, admin analytics, etc.). Without this hook the LMS
	Course filter wouldn't help — the Enrollment rows would still surface
	the soft-deleted course.

	Same bypass rules as LMSCourse: respect `lms_show_trash`, never filter
	during migrate/patch/install, defensively no-op if the LMS Course column
	doesn't exist yet.
	"""
	if frappe.flags.in_migrate or frappe.flags.in_patch or frappe.flags.in_install:
		return None
	if getattr(frappe.flags, "lms_show_trash", False):
		return None
	if not frappe.db.has_column("LMS Course", "is_deleted"):
		return None
	return (
		"EXISTS (SELECT 1 FROM `tabLMS Course` `c` "
		"WHERE `c`.`name` = `tabLMS Enrollment`.`course` "
		"AND `c`.`is_deleted` = 0)"
	)


@frappe.whitelist()
def send_course_enrollment_email(doc: Document):
	"""Whitelisted helper to (re-)send the course enrollment confirmation email.

	Accepts either an LMSEnrollment document or a JSON string so it can be
	called from the desk or from code paths that only have the raw values.
	"""
	if isinstance(doc, str):
		doc = frappe._dict(json.loads(doc))

	# Batch enrollments cascade into LMS Enrollment docs, but the batch already
	# sends its own confirmation email. Avoid duplicate/noisy course emails.
	if doc.get("enrollment_from_batch"):
		return

	if not doc.get("confirmation_email_sent"):
		outgoing_email_account = frappe.get_cached_value(
			"Email Account", {"default_outgoing": 1, "enable_outgoing": 1}, "name"
		)
		if outgoing_email_account or frappe.conf.get("mail_login"):
			try:
				send_course_enrollment_mail(doc)
				frappe.db.set_value("LMS Enrollment", doc.name, "confirmation_email_sent", 1)
			except Exception:
				# Don't fail the enrollment if only the notification email couldn't
				# be queued. The flag stays unset so admins can retry manually.
				frappe.log_error(
					_("Failed to send course enrollment confirmation email for {0}").format(
						doc.name
					),
					"Course Enrollment Email",
				)


def send_course_enrollment_mail(doc):
	course = frappe.db.get_value(
		"LMS Course",
		doc.course,
		["name", "title"],
		as_dict=1,
	)

	subject = _("Enrollment Confirmation for {0}").format(course.title)
	template = "course_enrollment"
	custom_template = frappe.db.get_single_value("LMS Settings", "course_enrollment_template")

	contact_email = frappe.db.get_single_value("LMS Settings", "contact_us_email")

	args = {
		"student_name": doc.member_name,
		"course_title": course.title,
		"course_name": course.name,
		"contact_email": contact_email,
	}

	if custom_template:
		email_template = get_email_template(custom_template, args)
		subject = email_template.get("subject")
		content = email_template.get("message")

	frappe.sendmail(
		recipients=doc.member,
		subject=subject,
		template=template if not custom_template else None,
		content=content if custom_template else None,
		args=args,
		header=[_(course.title), "green"],
		retry=3,
		now=True,
	)
