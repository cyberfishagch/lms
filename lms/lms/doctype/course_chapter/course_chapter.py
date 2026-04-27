# Copyright (c) 2021, FOSS United and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document

from lms.lms.utils import get_lesson_count


class CourseChapter(Document):
	def on_update(self):
		self.update_lesson_count()
		self.sync_chapter_reference()

	def on_trash(self):
		frappe.db.delete("Chapter Reference", {"chapter": self.name})

	def sync_chapter_reference(self):
		"""Ensure a Chapter Reference row exists in the course's child table.

		Mirrors sync_lesson_reference on Course Lesson. Required because
		get_chapters reads Chapter Reference rows under the course; a chapter
		created via a path that bypasses the parent LMS Course save would be
		invisible to the progress denominator.
		"""
		if not self.course:
			return

		before = self.get_doc_before_save()
		if before and before.course and before.course != self.course:
			frappe.db.delete(
				"Chapter Reference",
				{
					"parent": before.course,
					"parenttype": "LMS Course",
					"parentfield": "chapters",
					"chapter": self.name,
				},
			)

		existing = frappe.db.exists(
			"Chapter Reference",
			{
				"parent": self.course,
				"parenttype": "LMS Course",
				"parentfield": "chapters",
				"chapter": self.name,
			},
		)
		if existing:
			return

		max_idx = (
			frappe.db.sql(
				"""SELECT COALESCE(MAX(idx), 0) FROM `tabChapter Reference`
				WHERE parent = %s AND parenttype = 'LMS Course' AND parentfield = 'chapters'""",
				self.course,
			)[0][0]
			or 0
		)

		frappe.get_doc(
			{
				"doctype": "Chapter Reference",
				"parent": self.course,
				"parenttype": "LMS Course",
				"parentfield": "chapters",
				"chapter": self.name,
				"idx": max_idx + 1,
			}
		).insert(ignore_permissions=True)

	def update_lesson_count(self):
		"""Update lesson count in the course"""
		frappe.db.set_value("LMS Course", self.course, "lessons", get_lesson_count(self.course))
