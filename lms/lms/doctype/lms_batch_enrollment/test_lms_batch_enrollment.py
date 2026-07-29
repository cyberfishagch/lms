# Copyright (c) 2025, Frappe and Contributors
# See license.txt

from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase, UnitTestCase

from lms.lms.doctype.lms_batch_enrollment.lms_batch_enrollment import send_mail

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
			start_date=None,
			start_time=None,
			medium=None,
			confirmation_email_template=None,
		)

		with (
			patch.object(frappe.db, "get_value", return_value=batch),
			patch.object(frappe.db, "get_single_value", return_value=None),
			patch.object(frappe, "sendmail") as sendmail,
		):
			send_mail(enrollment)

		self.assertEqual(sendmail.call_args.kwargs["reference_doctype"], enrollment.doctype)
		self.assertEqual(sendmail.call_args.kwargs["reference_name"], enrollment.name)


class IntegrationTestLMSBatchEnrollment(IntegrationTestCase):
	"""
	Integration tests for LMSBatchEnrollment.
	Use this class for testing interactions between multiple components.
	"""

	pass
