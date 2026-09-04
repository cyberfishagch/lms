# Copyright (c) 2025, Frappe and Contributors
# See license.txt

from contextlib import nullcontext
from unittest import TestCase
from unittest.mock import Mock, patch

import frappe

from lms.lms.doctype.lms_batch_enrollment.lms_batch_enrollment import (
	_send_confirmation_email,
	send_confirmation_email,
	send_mail,
)
from lms.lms.test_helpers import BaseTestUtils

test_dependencies = ["Email Account"]


class TestLMSBatchEnrollment(TestCase):
	def test_confirmation_is_rechecked_inside_invitation_lock(self):
		enrollment = frappe._dict(
			doctype="LMS Batch Enrollment",
			name="test-batch-enrollment",
			member="learner@example.com",
		)

		with (
			patch(
				"lms.lms.doctype.lms_batch_enrollment.lms_batch_enrollment.student_invitation_lock",
				return_value=nullcontext(),
			),
			patch.object(frappe.db, "get_value", return_value=1) as get_value,
			patch.object(frappe, "get_cached_value") as get_cached_value,
		):
			_send_confirmation_email(enrollment)

		get_value.assert_called_once_with(enrollment.doctype, enrollment.name, "confirmation_email_sent")
		get_cached_value.assert_not_called()

	def test_manual_resend_loads_document_and_checks_permission(self):
		enrollment = Mock()
		with (
			patch.object(frappe, "get_doc", return_value=enrollment) as get_doc,
			patch(
				"lms.lms.doctype.lms_batch_enrollment.lms_batch_enrollment._send_confirmation_email"
			) as send_email,
		):
			send_confirmation_email("test-enrollment")

		get_doc.assert_called_once_with("LMS Batch Enrollment", "test-enrollment")
		enrollment.check_permission.assert_called_once_with("write")
		send_email.assert_called_once_with(enrollment)

	def test_confirmation_email_references_batch_enrollment(self):
		enrollment = frappe._dict(
			doctype="LMS Batch Enrollment",
			name="test-batch-enrollment",
			batch="test-batch",
			member="learner@example.com",
			member_name="Test Learner",
		)
		batch = frappe._dict(
			name="test-batch",
			title="Test Batch",
			end_date="2026-08-10",
			start_date=None,
			start_time=None,
			medium=None,
			confirmation_email_template=None,
		)
		courses = [frappe._dict(course="test-course", title="Test Course")]

		with (
			patch.object(frappe.db, "get_value", return_value=batch),
			patch.object(frappe.db, "get_single_value", return_value=None),
			patch.object(frappe, "get_all", return_value=courses),
			patch(
				"lms.lms.doctype.lms_batch_enrollment.lms_batch_enrollment.get_url",
				return_value="https://matchbox.training",
			),
			patch(
				"lms.lms.doctype.lms_batch_enrollment.lms_batch_enrollment.get_student_password_setup_url",
				return_value=None,
			),
			patch(
				"lms.lms.doctype.lms_batch_enrollment.lms_batch_enrollment.send_student_password_setup_email"
			) as send_setup,
			patch.object(frappe, "sendmail") as sendmail,
		):
			self.assertFalse(send_mail(enrollment))

		mail = sendmail.call_args.kwargs
		self.assertEqual(mail["subject"], "Training Assignment: Test Course")
		self.assertEqual(mail["template"], "batch_confirmation")
		self.assertEqual(mail["args"]["course_titles"], ["Test Course"])
		self.assertEqual(mail["args"]["end_date"], "2026-08-10")
		self.assertEqual(mail["args"]["login_url"], "https://matchbox.training")
		self.assertEqual(mail["reference_doctype"], enrollment.doctype)
		self.assertEqual(mail["reference_name"], enrollment.name)
		self.assertFalse(mail["now"])
		send_setup.assert_not_called()

	def test_empty_batch_assignment_includes_deferred_password_setup(self):
		enrollment = frappe._dict(
			doctype="LMS Batch Enrollment",
			name="test-empty-batch-enrollment",
			batch="test-empty-batch",
			member="learner@example.com",
			member_name="Test Learner",
		)
		batch = frappe._dict(
			name="test-empty-batch",
			title="Test Empty Batch",
			end_date=None,
			start_date=None,
			start_time=None,
			medium=None,
			confirmation_email_template=None,
		)
		setup_url = "https://matchbox.training/update-password?key=test"

		with (
			patch.object(frappe.db, "get_value", return_value=batch),
			patch.object(frappe.db, "get_single_value", return_value=None),
			patch.object(frappe, "get_all", return_value=[]),
			patch(
				"lms.lms.doctype.lms_batch_enrollment.lms_batch_enrollment.get_url",
				return_value="https://matchbox.training",
			),
			patch(
				"lms.lms.doctype.lms_batch_enrollment.lms_batch_enrollment.get_student_password_setup_url",
				return_value=setup_url,
			),
			patch(
				"lms.lms.doctype.lms_batch_enrollment.lms_batch_enrollment.send_student_password_setup_email",
				return_value=True,
			) as send_setup,
		):
			self.assertTrue(send_mail(enrollment))

		mail = send_setup.call_args.kwargs
		self.assertEqual(mail["subject"], "Matchbox Training Assignment")
		self.assertEqual(mail["args"]["course_titles"], [])
		self.assertEqual(mail["args"]["password_setup_url"], setup_url)
		send_setup.assert_called_once_with(enrollment.member, setup_url, **mail)

	def test_confirmation_email_failure_keeps_batch_retryable(self):
		enrollment = frappe._dict(
			doctype="LMS Batch Enrollment",
			name="test-batch-enrollment",
			member="learner@example.com",
			confirmation_email_sent=0,
		)

		with (
			patch(
				"lms.lms.doctype.lms_batch_enrollment.lms_batch_enrollment.student_invitation_lock",
				return_value=nullcontext(),
			),
			patch.object(frappe.db, "get_value", return_value=0),
			patch.object(frappe, "get_cached_value", return_value="outgoing"),
			patch(
				"lms.lms.doctype.lms_batch_enrollment.lms_batch_enrollment.send_mail",
				side_effect=RuntimeError("queue failed"),
			),
			patch.object(frappe, "log_error") as log_error,
			patch.object(frappe.db, "set_value") as set_value,
		):
			_send_confirmation_email(enrollment)

		set_value.assert_not_called()
		log_error.assert_called_once()


class IntegrationTestLMSBatchEnrollment(BaseTestUtils):
	def test_deferred_student_receives_batch_assignment_email(self):
		instructor = self._create_user(
			f"batch-instructor-{frappe.generate_hash()}@example.com",
			"Batch",
			"Instructor",
			["Moderator", "Course Creator", "Batch Evaluator"],
		)
		student = self._create_user(
			f"batch-student-{frappe.generate_hash()}@example.com",
			"Batch",
			"Student",
			["LMS Student"],
		)
		course = self._create_course(
			f"Batch invitation course {frappe.generate_hash()}",
			instructor.name,
		)
		evaluator = self._create_evaluator(instructor.name)
		batch = self._create_batch(
			course.name,
			instructor=instructor.name,
			title=f"Batch invitation {frappe.generate_hash()}",
			evaluator=evaluator.name,
		)
		frappe.db.set_value(
			"Email Account",
			"_Test Email Account 1",
			{"enable_outgoing": 1, "default_outgoing": 1},
		)
		frappe.clear_cache(doctype="Email Account")

		frappe.set_user("Administrator")

		def fail_on_email_error(*args, **kwargs):
			raise AssertionError(frappe.get_traceback())

		with (
			patch(
				"frappe.email.doctype.email_queue.email_queue.SendMailContext.fetch_smtp_server",
				new=lambda context: setattr(
					context,
					"email_account_doc",
					context.queue_doc.get_email_account(raise_error=True),
				),
			),
			patch.object(frappe, "log_error", side_effect=fail_on_email_error),
		):
			enrollment = self._create_batch_enrollment(student.name, batch.name)

		email_queue = frappe.get_last_doc(
			"Email Queue",
			filters={
				"reference_doctype": enrollment.doctype,
				"reference_name": enrollment.name,
			},
		)
		self.assertEqual(email_queue.status, "Sent")
		self.assertEqual(
			frappe.db.get_value(enrollment.doctype, enrollment.name, "confirmation_email_sent"),
			1,
		)
		self.assertEqual(frappe.db.get_value("User", student.name, "send_welcome_email"), 1)
