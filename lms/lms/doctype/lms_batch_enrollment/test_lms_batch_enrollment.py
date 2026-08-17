# Copyright (c) 2025, Frappe and Contributors
# See license.txt

from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase, UnitTestCase

from lms.lms.doctype.lms_batch_enrollment.lms_batch_enrollment import (
	send_confirmation_email,
	send_mail,
)

# On IntegrationTestCase, the doctype test records and all
# link-field test record dependencies are recursively loaded
# Use these module variables to add/remove to/from that list
EXTRA_TEST_RECORD_DEPENDENCIES = []  # eg. ["User"]
IGNORE_TEST_RECORD_DEPENDENCIES = []  # eg. ["User"]


class UnitTestLMSBatchEnrollment(UnitTestCase):
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
				"lms.lms.doctype.lms_batch_enrollment.lms_batch_enrollment.mark_student_welcome_sent"
			) as mark_sent,
			patch.object(frappe, "sendmail") as sendmail,
		):
			send_mail(enrollment)

		mail = sendmail.call_args.kwargs
		self.assertEqual(mail["subject"], "Training Assignment: Test Course")
		self.assertEqual(mail["template"], "batch_confirmation")
		self.assertEqual(mail["args"]["course_titles"], ["Test Course"])
		self.assertEqual(mail["args"]["end_date"], "2026-08-10")
		self.assertEqual(mail["args"]["login_url"], "https://matchbox.training")
		self.assertEqual(mail["reference_doctype"], enrollment.doctype)
		self.assertEqual(mail["reference_name"], enrollment.name)
		self.assertFalse(mail["now"])
		mark_sent.assert_not_called()

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
			patch.object(frappe.db, "get_value", side_effect=[batch, "Sent"]),
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
				"lms.lms.doctype.lms_batch_enrollment.lms_batch_enrollment.mark_student_welcome_sent"
			) as mark_sent,
			patch.object(frappe, "sendmail", return_value=frappe._dict(name="test-email")) as sendmail,
		):
			send_mail(enrollment)

		mail = sendmail.call_args.kwargs
		self.assertEqual(mail["subject"], "Matchbox Training Assignment")
		self.assertEqual(mail["args"]["course_titles"], [])
		self.assertEqual(mail["args"]["password_setup_url"], setup_url)
		self.assertTrue(mail["now"])
		mark_sent.assert_called_once_with(enrollment.member)

	def test_confirmation_email_failure_keeps_batch_retryable(self):
		enrollment = frappe._dict(
			doctype="LMS Batch Enrollment",
			name="test-batch-enrollment",
			confirmation_email_sent=0,
		)

		with (
			patch.object(frappe, "get_cached_value", return_value="outgoing"),
			patch(
				"lms.lms.doctype.lms_batch_enrollment.lms_batch_enrollment.send_mail",
				side_effect=RuntimeError("queue failed"),
			),
			patch.object(frappe, "log_error") as log_error,
			patch.object(frappe.db, "set_value") as set_value,
		):
			send_confirmation_email(enrollment)

		set_value.assert_not_called()
		log_error.assert_called_once()


class IntegrationTestLMSBatchEnrollment(IntegrationTestCase):
	"""
	Integration tests for LMSBatchEnrollment.
	Use this class for testing interactions between multiple components.
	"""

	pass
