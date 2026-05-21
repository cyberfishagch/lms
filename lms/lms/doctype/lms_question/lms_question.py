# Copyright (c) 2023, Frappe and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import now_datetime

from lms.lms.utils import has_course_instructor_role, has_moderator_role


class LMSQuestion(Document):
	def before_insert(self):
		_sync_series_to_max_existing()

	def validate(self):
		validate_correct_answers(self)
		update_question_title(self)


def _sync_series_to_max_existing():
	"""Walk the Series('') counter forward to the real max existing
	LMS Question name suffix before autoname runs.

	The doctype's ``format:QTS-{YYYY}-{#####}`` autoname uses Frappe's
	*global* ``Series('')`` counter — shared with every other doctype that
	uses a ``{#####}`` placeholder (see frappe/tests/test_naming.py:103).
	That counter drifts below the real max whenever records are inserted
	by fixtures, migrations, or sibling format-autoname doctypes. The next
	autoname-driven insert then collides — surfacing as the production
	error "LMS Question QTS-2026-00109 already exists" both in clone_quiz
	and in the course-creation wizard's Step 3.

	Running this in ``before_insert`` covers every insert path (wizard,
	clone_quiz, admin UI, bench fixtures) without needing per-callsite
	retry wrappers. ``_insert_with_naming_retry`` in api.py stays as a
	defence in depth for the bulk-clone path under concurrency.
	"""
	year = now_datetime().strftime("%Y")
	prefix = f"QTS-{year}-"

	rows = frappe.db.sql(
		"""SELECT MAX(CAST(SUBSTRING(name, %s) AS UNSIGNED))
		   FROM `tabLMS Question` WHERE name LIKE %s""",
		(len(prefix) + 1, prefix + "%"),
	)
	max_suffix = (rows[0][0] if rows and rows[0] else None) or 0
	if max_suffix == 0:
		return

	# Use raw SQL — tabSeries has no `modified` column, so frappe.db.set_value
	# fails with "Unknown column 'modified'".
	existing = frappe.db.sql("SELECT current FROM `tabSeries` WHERE name = ''")
	current_val = (existing[0][0] if existing and existing[0] else None) or 0
	if existing:
		if current_val < max_suffix:
			frappe.db.sql(
				"UPDATE `tabSeries` SET `current` = %s WHERE `name` = ''",
				(max_suffix,),
			)
	else:
		frappe.db.sql(
			"INSERT INTO `tabSeries` (`name`, `current`) VALUES ('', %s)",
			(max_suffix,),
		)


def validate_correct_answers(question):
	if question.type == "Choices":
		validate_duplicate_options(question)
		validate_minimum_options(question)
		validate_correct_options(question)
	elif question.type == "User Input":
		validate_possible_answer(question)


def validate_duplicate_options(question):
	options = []

	for num in range(1, 5):
		if question.get(f"option_{num}"):
			options.append(question.get(f"option_{num}"))

	if len(set(options)) != len(options):
		frappe.throw(_("Duplicate options found for this question."))


def validate_correct_options(question):
	correct_options = get_correct_options(question)

	if len(correct_options) > 1:
		question.multiple = 1
	else:
		question.multiple = 0

	if not len(correct_options):
		frappe.throw(_("At least one option must be correct for this question."))


def validate_minimum_options(question):
	if question.type == "Choices" and (not question.option_1 or not question.option_2):
		frappe.throw(_("Minimum two options are required for multiple choice questions."))


def validate_possible_answer(question):
	possible_answers = []
	possible_answers_fields = [
		"possibility_1",
		"possibility_2",
		"possibility_3",
		"possibility_4",
	]

	for field in possible_answers_fields:
		if question.get(field):
			possible_answers.append(field)

	if not len(possible_answers):
		frappe.throw(
			_("Add at least one possible answer for this question: {0}").format(
				frappe.bold(question.question)
			)
		)


def update_question_title(question):
	if not question.is_new():
		question_rows = frappe.get_all("LMS Quiz Question", {"question": question.name}, pluck="name")

		for row in question_rows:
			frappe.db.set_value("LMS Quiz Question", row, "question_detail", question.question)


def get_correct_options(question):
	correct_options = []
	correct_option_fields = [
		"is_correct_1",
		"is_correct_2",
		"is_correct_3",
		"is_correct_4",
	]
	for field in correct_option_fields:
		if question.get(field) == 1:
			correct_options.append(field)

	return correct_options
