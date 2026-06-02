# Copyright (c) 2021, FOSS United and Contributors
# See license.txt

# import frappe
import unittest

import frappe

from lms.lms.api import delete_quiz


class TestLMSQuiz(unittest.TestCase):
	@classmethod
	def setUpClass(cls) -> None:
		frappe.get_doc({"doctype": "LMS Quiz", "title": "Test Quiz", "passing_percentage": 90}).save()

	def test_delete_quiz_detaches_submissions(self):
		"""Regression: a quiz with submissions used to fail deletion with
		LinkExistsError (frappe.client.delete via the admin UI hit the link
		integrity check on LMS Quiz Submission.quiz). delete_quiz now
		detaches submissions (NULL out the quiz link) so the row goes away
		while learner attempt history survives."""
		quiz = frappe.get_doc(
			{
				"doctype": "LMS Quiz",
				"title": f"Test Delete Quiz {frappe.generate_hash(length=6)}",
				"passing_percentage": 50,
			}
		).insert()

		submission = frappe.get_doc(
			{
				"doctype": "LMS Quiz Submission",
				"quiz": quiz.name,
				"member": frappe.session.user,
				"score": 0,
				"score_out_of": 0,
				"percentage": 0,
			}
		).insert()

		delete_quiz(quiz.name)

		self.assertFalse(frappe.db.exists("LMS Quiz", quiz.name))
		self.assertTrue(frappe.db.exists("LMS Quiz Submission", submission.name))
		self.assertIsNone(frappe.db.get_value("LMS Quiz Submission", submission.name, "quiz"))

		frappe.delete_doc("LMS Quiz Submission", submission.name, force=1)

	def test_with_multiple_options(self):
		question = frappe.new_doc("LMS Question")
		question.question = "Question Multiple"
		question.type = "Choices"
		question.option_1 = "Option 1"
		question.is_correct_1 = 1
		question.option_2 = "Option 2"
		question.is_correct_2 = 1
		question.save()
		self.assertTrue(question.multiple)

	def test_with_no_correct_option(self):
		question = frappe.new_doc("LMS Question")
		question.question = "Question Multiple"
		question.type = "Choices"
		question.option_1 = "Option 1"
		question.option_2 = "Option 2"
		self.assertRaises(frappe.ValidationError, question.save)

	def test_with_no_possible_answers(self):
		question = frappe.new_doc("LMS Question")
		question.question = "Question Multiple"
		question.type = "User Input"
		self.assertRaises(frappe.ValidationError, question.save)

	@classmethod
	def tearDownClass(cls) -> None:
		frappe.db.delete("LMS Quiz", "test-quiz")
		frappe.db.delete("LMS Question")
