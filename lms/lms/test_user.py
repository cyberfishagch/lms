from datetime import datetime, timedelta
from unittest import TestCase
from unittest.mock import Mock, patch

import frappe
from frappe.utils.data import sha256_hash

from lms.lms.user import _resend_expired_password_link


class TestUser(TestCase):
	def setUp(self):
		self.db = Mock()
		frappe.local.db = self.db

	def tearDown(self):
		frappe.local.__release_local__()

	def test_resends_password_email_for_expired_key(self):
		now = datetime(2026, 8, 17, 12, 0, 0)
		key = "expired-key"
		hashed_key = sha256_hash(key)
		user = frappe._dict(
			name="learner@example.com",
			enabled=1,
			reset_password_key=hashed_key,
			last_reset_password_key_generated_on=now - timedelta(seconds=7201),
			validate_reset_password=Mock(),
			reset_password=Mock(),
		)

		self.db.get_value.return_value = user.name
		self.db.get_single_value.return_value = 7200
		with (
			patch.object(frappe, "get_doc", return_value=user) as get_doc,
			patch("lms.lms.user.now_datetime", return_value=now),
		):
			_resend_expired_password_link(key)

		get_doc.assert_called_once_with("User", user.name, for_update=True)
		user.validate_reset_password.assert_called_once_with()
		user.reset_password.assert_called_once_with(send_email=True)

	def test_does_not_resend_for_unexpired_key(self):
		now = datetime(2026, 8, 17, 12, 0, 0)
		key = "current-key"
		user = frappe._dict(
			name="learner@example.com",
			enabled=1,
			reset_password_key=sha256_hash(key),
			last_reset_password_key_generated_on=now - timedelta(seconds=7199),
			validate_reset_password=Mock(),
			reset_password=Mock(),
		)

		self.db.get_value.return_value = user.name
		self.db.get_single_value.return_value = 7200
		with (
			patch.object(frappe, "get_doc", return_value=user),
			patch("lms.lms.user.now_datetime", return_value=now),
		):
			_resend_expired_password_link(key)

		user.validate_reset_password.assert_not_called()
		user.reset_password.assert_not_called()

	def test_does_not_resend_for_unknown_key(self):
		self.db.get_value.return_value = None
		with patch.object(frappe, "get_doc") as get_doc:
			_resend_expired_password_link("unknown-key")

		get_doc.assert_not_called()
