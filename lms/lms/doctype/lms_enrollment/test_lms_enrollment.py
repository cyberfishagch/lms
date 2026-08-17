# Copyright (c) 2021, FOSS United and Contributors
# See license.txt

from unittest.mock import Mock, patch

import frappe
from frappe.tests import UnitTestCase

from lms.lms.doctype.lms_enrollment.lms_enrollment import send_course_enrollment_mail
from lms.lms.student_invitation import get_student_password_setup_url


class UnitTestLMSEnrollment(UnitTestCase):
	def test_direct_course_assignment_includes_deferred_password_setup(self):
		enrollment = frappe._dict(
			course="test-course",
			member="learner@example.com",
			member_name="Test Learner",
		)
		course = frappe._dict(name="test-course", title="Test Course")
		setup_url = "https://matchbox.training/update-password?key=test"

		with (
			patch.object(frappe.db, "get_value", return_value=course),
			patch.object(frappe.db, "get_single_value", return_value=None),
			patch(
				"lms.lms.doctype.lms_enrollment.lms_enrollment.get_student_password_setup_url",
				return_value=setup_url,
			),
			patch(
				"lms.lms.doctype.lms_enrollment.lms_enrollment.get_url",
				return_value="https://matchbox.training",
			),
			patch("lms.lms.doctype.lms_enrollment.lms_enrollment.mark_student_welcome_sent") as mark_sent,
			patch.object(frappe, "sendmail") as sendmail,
		):
			send_course_enrollment_mail(enrollment)

		mail = sendmail.call_args.kwargs
		self.assertEqual(mail["template"], "course_enrollment")
		self.assertEqual(mail["args"]["password_setup_url"], setup_url)
		mark_sent.assert_called_once_with(enrollment.member)

	def test_later_course_assignment_does_not_repeat_password_setup(self):
		enrollment = frappe._dict(
			course="test-course",
			member="learner@example.com",
			member_name="Test Learner",
		)
		course = frappe._dict(name="test-course", title="Test Course")

		with (
			patch.object(frappe.db, "get_value", return_value=course),
			patch.object(frappe.db, "get_single_value", return_value=None),
			patch(
				"lms.lms.doctype.lms_enrollment.lms_enrollment.get_student_password_setup_url",
				return_value=None,
			),
			patch(
				"lms.lms.doctype.lms_enrollment.lms_enrollment.get_url",
				return_value="https://matchbox.training",
			),
			patch("lms.lms.doctype.lms_enrollment.lms_enrollment.mark_student_welcome_sent") as mark_sent,
			patch.object(frappe, "sendmail") as sendmail,
		):
			send_course_enrollment_mail(enrollment)

		self.assertIsNone(sendmail.call_args.kwargs["args"]["password_setup_url"])
		mark_sent.assert_not_called()

	def test_password_setup_is_only_generated_for_deferred_students(self):
		user = Mock(send_welcome_email=0)
		user.reset_password.return_value = "https://matchbox.training/update-password?key=test"

		with (
			patch.object(frappe.db, "exists", return_value=False),
			patch.object(frappe, "get_doc") as get_doc,
		):
			self.assertIsNone(get_student_password_setup_url("learner@example.com"))
			get_doc.assert_not_called()

		user.send_welcome_email = 1
		with (
			patch.object(frappe.db, "exists", return_value=True),
			patch.object(frappe, "get_doc", return_value=user) as get_doc,
		):
			self.assertIsNone(get_student_password_setup_url("learner@example.com"))
			get_doc.assert_called_once_with("User", "learner@example.com", for_update=True)
			user.reset_password.assert_not_called()

		user.send_welcome_email = 0
		with (
			patch.object(frappe.db, "exists", return_value=True),
			patch.object(frappe, "get_doc", return_value=user),
			patch("lms.lms.student_invitation._user_has_password", return_value=True),
		):
			self.assertIsNone(get_student_password_setup_url("learner@example.com"))
			user.reset_password.assert_not_called()

		with (
			patch.object(frappe.db, "exists", return_value=True),
			patch.object(frappe, "get_doc", return_value=user),
			patch("lms.lms.student_invitation._user_has_password", return_value=False),
		):
			self.assertEqual(
				get_student_password_setup_url("learner@example.com"),
				"https://matchbox.training/update-password?key=test",
			)
			user.reset_password.assert_called_once_with()
