# Copyright (c) 2021, FOSS United and Contributors
# See license.txt

from contextlib import nullcontext
from unittest import TestCase
from unittest.mock import Mock, patch

import frappe
from frappe.core.doctype.user.user import User
from frappe.utils import sha256_hash

from lms.lms.doctype.lms_enrollment.lms_enrollment import (
	_send_course_enrollment_email,
	send_course_enrollment_email,
	send_course_enrollment_mail,
)
from lms.lms.student_invitation import (
	abandon_student_password_setup,
	complete_student_password_setup,
	ensure_password_setup_email_sent,
	get_student_password_setup_url,
	send_student_password_setup_email,
)


class TestLMSEnrollment(TestCase):
	def test_confirmation_is_rechecked_inside_invitation_lock(self):
		enrollment = frappe._dict(
			name="test-enrollment",
			member="learner@example.com",
			enrollment_from_batch=None,
		)

		with (
			patch(
				"lms.lms.doctype.lms_enrollment.lms_enrollment.student_invitation_lock",
				return_value=nullcontext(),
			),
			patch.object(frappe.db, "get_value", return_value=1) as get_value,
			patch.object(frappe, "get_cached_value") as get_cached_value,
		):
			_send_course_enrollment_email(enrollment)

		get_value.assert_called_once_with("LMS Enrollment", enrollment.name, "confirmation_email_sent")
		get_cached_value.assert_not_called()

	def test_invitation_lock_failure_does_not_block_enrollment(self):
		enrollment = frappe._dict(
			name="test-enrollment",
			member="learner@example.com",
			enrollment_from_batch=None,
		)

		with (
			patch(
				"lms.lms.doctype.lms_enrollment.lms_enrollment.student_invitation_lock",
				side_effect=RuntimeError("lock unavailable"),
			),
			patch.object(frappe, "log_error") as log_error,
		):
			_send_course_enrollment_email(enrollment)

		log_error.assert_called_once()

	def test_manual_resend_loads_document_and_checks_permission(self):
		enrollment = Mock()
		with (
			patch.object(frappe, "get_doc", return_value=enrollment) as get_doc,
			patch(
				"lms.lms.doctype.lms_enrollment.lms_enrollment._send_course_enrollment_email"
			) as send_email,
		):
			send_course_enrollment_email("test-enrollment")

		get_doc.assert_called_once_with("LMS Enrollment", "test-enrollment")
		enrollment.check_permission.assert_called_once_with("write")
		send_email.assert_called_once_with(enrollment)

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
			patch(
				"lms.lms.doctype.lms_enrollment.lms_enrollment.send_student_password_setup_email",
				return_value=True,
			) as send_setup,
		):
			self.assertTrue(send_course_enrollment_mail(enrollment))

		mail = send_setup.call_args.kwargs
		self.assertEqual(mail["template"], "course_enrollment")
		self.assertEqual(mail["args"]["password_setup_url"], setup_url)
		send_setup.assert_called_once_with(enrollment.member, setup_url, **mail)

	def test_password_setup_rejects_unsent_email_queue(self):
		with patch.object(frappe.db, "get_value", return_value="Not Sent"):
			with self.assertRaises(frappe.OutgoingEmailError):
				ensure_password_setup_email_sent(
					frappe._dict(name="test-email"),
					"https://matchbox.training/update-password?key=test",
				)

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
			patch(
				"lms.lms.doctype.lms_enrollment.lms_enrollment.send_student_password_setup_email"
			) as send_setup,
			patch.object(frappe, "sendmail") as sendmail,
		):
			self.assertFalse(send_course_enrollment_mail(enrollment))

		self.assertIsNone(sendmail.call_args.kwargs["args"]["password_setup_url"])
		self.assertTrue(sendmail.call_args.kwargs["now"])
		send_setup.assert_not_called()

	def test_password_setup_delivery_uses_retained_queue(self):
		setup_url = "https://matchbox.training/update-password?key=secret"
		email_queue = Mock()

		with (
			patch.object(frappe, "sendmail", return_value=email_queue) as sendmail,
			patch(
				"lms.lms.student_invitation.complete_student_password_setup", return_value=True
			) as complete,
		):
			self.assertTrue(
				send_student_password_setup_email(
					"learner@example.com",
					setup_url,
					recipients="learner@example.com",
					subject="Training Assignment",
				)
			)

		sendmail.assert_called_once_with(
			now=False,
			recipients="learner@example.com",
			subject="Training Assignment",
		)
		email_queue.send.assert_called_once_with()
		complete.assert_called_once_with("learner@example.com", email_queue, setup_url)

	def test_failed_password_setup_delivery_is_abandoned(self):
		setup_url = "https://matchbox.training/update-password?key=secret"
		email_queue = Mock()
		email_queue.send.side_effect = RuntimeError("smtp failed")

		with (
			patch.object(frappe, "sendmail", return_value=email_queue),
			patch("lms.lms.student_invitation.abandon_student_password_setup") as abandon,
			self.assertRaisesRegex(RuntimeError, "smtp failed"),
		):
			send_student_password_setup_email("learner@example.com", setup_url)

		abandon.assert_called_once_with("learner@example.com", email_queue, setup_url)

	def test_abandoned_setup_clears_only_its_key_and_quarantines_queue(self):
		setup_url = "https://matchbox.training/update-password?key=secret"
		email_queue = frappe._dict(
			name="test-email",
			message=f"Subject: Training\r\n\r\nOpen {setup_url}",
		)

		with (
			patch.object(frappe.db, "set_value") as set_value,
			patch.object(frappe.db, "commit") as commit,
		):
			abandon_student_password_setup("learner@example.com", email_queue, setup_url)

		user_update = set_value.call_args_list[0]
		self.assertEqual(user_update.args[0], "User")
		self.assertEqual(user_update.args[1]["name"], "learner@example.com")
		self.assertEqual(
			user_update.args[1]["reset_password_key"],
			sha256_hash("secret"),
		)
		self.assertEqual(
			user_update.args[2],
			{"reset_password_key": "", "last_reset_password_key_generated_on": None},
		)
		self.assertNotIn(setup_url, set_value.call_args_list[1].args[3])
		self.assertEqual(set_value.call_args_list[2].args[2]["status"], "Error")
		commit.assert_called_once_with()

	def test_successful_setup_is_redacted_and_marked_sent(self):
		setup_url = "https://matchbox.training/update-password?key=secret"
		email_queue = frappe._dict(
			name="test-email",
			message=f"Subject: Training\r\nMIME-Version: 1.0\r\n\r\nOpen {setup_url}",
		)

		with (
			patch.object(frappe.db, "get_value", return_value="Sent"),
			patch.object(frappe.db, "set_value") as set_value,
		):
			self.assertTrue(complete_student_password_setup("learner@example.com", email_queue, setup_url))

		redacted_message = set_value.call_args_list[0].args[3]
		self.assertNotIn(setup_url, redacted_message)
		self.assertIn("Subject: Training", redacted_message)
		self.assertEqual(
			set_value.call_args_list[1].args,
			("User", "learner@example.com", "send_welcome_email", 1),
		)

	def test_password_setup_is_only_generated_for_deferred_students(self):
		user = Mock(spec=User)
		user.name = "learner@example.com"
		user.enabled = 1
		user.send_welcome_email = 0
		user.reset_password_key = None
		user.last_reset_password_key_generated_on = None
		user._reset_password.return_value = "https://matchbox.training/update-password?key=test"

		with (
			patch.object(frappe.db, "exists", return_value=False),
			patch.object(frappe, "get_doc") as get_doc,
		):
			self.assertIsNone(get_student_password_setup_url("learner@example.com"))
			get_doc.assert_not_called()

		user.send_welcome_email = 1
		with (
			patch.object(frappe.db, "exists", side_effect=[True, False]),
			patch.object(frappe, "get_doc", return_value=user) as get_doc,
		):
			self.assertIsNone(get_student_password_setup_url("learner@example.com"))
			get_doc.assert_called_once_with("User", "learner@example.com", for_update=True)
			user._reset_password.assert_not_called()

		user.send_welcome_email = 0
		with (
			patch.object(frappe.db, "exists", side_effect=[True, False]),
			patch.object(frappe, "get_doc", return_value=user),
			patch("lms.lms.student_invitation._user_has_password", return_value=True),
		):
			self.assertIsNone(get_student_password_setup_url("learner@example.com"))
			user._reset_password.assert_not_called()

		with (
			patch.object(frappe.db, "exists", side_effect=[True, False]),
			patch.object(frappe, "get_doc", return_value=user),
			patch("lms.lms.student_invitation._user_has_password", return_value=False),
		):
			self.assertEqual(
				get_student_password_setup_url("learner@example.com"),
				"https://matchbox.training/update-password?key=test",
			)
			user._reset_password.assert_called_once_with()

	def test_admin_role_and_active_key_both_prevent_rotation(self):
		user = Mock(spec=User)
		user.name = "learner@example.com"
		user.enabled = 1
		user.send_welcome_email = 0
		user.reset_password_key = "existing-hash"
		user.last_reset_password_key_generated_on = None

		with (
			patch.object(frappe.db, "exists", side_effect=[True, True]),
			patch.object(frappe, "get_doc") as get_doc,
		):
			self.assertIsNone(get_student_password_setup_url(user.name))
			get_doc.assert_not_called()

		with (
			patch.object(frappe.db, "exists", side_effect=[True, False]),
			patch.object(frappe.db, "get_single_value", return_value=7200),
			patch.object(frappe, "get_doc", return_value=user),
			patch("lms.lms.student_invitation._user_has_password", return_value=False),
		):
			self.assertIsNone(get_student_password_setup_url(user.name))
			user._reset_password.assert_not_called()
