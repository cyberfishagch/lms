# Copyright (c) 2021, FOSS United and Contributors
# See license.txt

import json
import unittest

import frappe

from lms.lms.test_helpers import BaseTestUtils


class TestCourseLesson(unittest.TestCase):
	pass


class TestLessonContentSurvivesSave(BaseTestUtils):
	r"""`content` holds JSON, not HTML.

	frappe's field-level `sanitize_html` used to run over the whole envelope and
	rewrite every `\"` inside it to `\&quot;`, so any inline tool that emits an
	attribute (link, inline code, colour) left the field unparseable and the
	lesson body unreadable. `ignore_xss_filter` on the field stops that; the real
	gate is `sanitize_editorjs`, which walks the parsed document string by string.
	"""

	# Kept verbatim by the sanitiser. A link is not here: nh3 deliberately adds
	# rel="noopener noreferrer" to every <a>, so it is asserted separately below.
	ATTRIBUTE_MARKUP = {
		"inline_code": '<code class="inline-code">code</code>',
		"colour": '<span class="lms-inline-color" style="color:rgb(255, 0, 0)">tint</span>',
	}

	def setUp(self):
		super().setUp()
		# _create_course() defaults instructor="frappe@example.com"; create it so the
		# course's instructor Link resolves on a fresh DB.
		self._create_user("frappe@example.com", "Frappe", "Admin", ["Moderator", "Course Creator"])
		self.course = self._create_course(title="Inline Markup Course")
		self.chapter = self._create_chapter("Inline Markup Chapter", self.course.name)

	def _saved_text(self, markup, title):
		content = json.dumps({"blocks": [{"id": "b1", "type": "paragraph", "data": {"text": markup}}]})
		lesson = self._create_lesson(title, self.chapter.name, self.course.name, content)
		stored = frappe.db.get_value("Course Lesson", lesson.name, "content")
		return json.loads(stored)["blocks"][0]["data"]["text"]

	def test_field_is_exempt_from_frappe_html_sanitiser(self):
		"""Guards the docfield property the rest of this class depends on."""
		meta = frappe.get_meta("Course Lesson")
		for fieldname in ("content", "instructor_content"):
			self.assertTrue(
				meta.get_field(fieldname).get("ignore_xss_filter"),
				f"{fieldname} must carry ignore_xss_filter; run bench migrate",
			)

	def test_attribute_bearing_inline_markup_round_trips(self):
		for name, markup in self.ATTRIBUTE_MARKUP.items():
			with self.subTest(markup=name):
				self.assertEqual(self._saved_text(markup, f"Lesson {name}"), markup)

	def test_link_keeps_its_href_and_gains_rel(self):
		saved = self._saved_text('<a href="https://frappe.io/">here</a>', "Lesson link")
		self.assertIn('href="https://frappe.io/"', saved)
		self.assertIn('rel="noopener noreferrer"', saved)

	def test_plain_inline_markup_round_trips(self):
		markup = "<b>b</b> <i>i</i> <u>u</u> <s>s</s>"
		self.assertEqual(self._saved_text(markup, "Lesson plain"), markup)

	def test_script_and_event_handlers_are_still_stripped(self):
		saved = self._saved_text('<script>alert(1)</script><img src="x" onerror="alert(1)">ok', "Lesson xss")
		self.assertNotIn("<script", saved)
		self.assertNotIn("onerror", saved)
		self.assertIn("ok", saved)
