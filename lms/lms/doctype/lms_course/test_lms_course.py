# Copyright (c) 2021, FOSS United and Contributors
# See license.txt

import frappe

from lms.lms.api import delete_course
from lms.lms.test_helpers import BaseTestUtils


class TestLMSCourse(BaseTestUtils):
	def setUp(self):
		super().setUp()
		self.instructor = self._create_user(
			"frappe@example.com", "Frappe", "Admin", ["Moderator", "Course Creator"]
		)

	def test_new_course(self):
		course_name = f"Test Course {frappe.generate_hash()}"

		course = self._create_course(course_name)

		self.assertEqual(course.title, course_name)
		self.assertTrue(frappe.db.exists("LMS Course", course.name))

	def test_delete_course(self):
		course = self._create_course(f"Test Course {frappe.generate_hash()}")
		chapter = self._create_chapter(f"Test Chapter {frappe.generate_hash()}", course.name)
		lesson = self._create_lesson(f"Test Lesson {frappe.generate_hash()}", chapter.name, course.name)

		lesson_ref = self._create_lesson_reference(chapter.name, lesson.name)
		chapter_ref = self._create_chapter_reference(course.name, chapter.name)

		user_email = f"test_{frappe.generate_hash()}@example.com"
		self._create_user(user_email, "Test", "Member", ["LMS Student"])
		enrollment = self._create_enrollment(user_email, course.name)
		progress = self._create_progress(user_email, course.name, lesson.name)

		delete_course(course.name)

		self.assertFalse(frappe.db.exists("LMS Course", course.name))
		self.assertFalse(frappe.db.exists("Course Chapter", chapter.name))
		self.assertFalse(frappe.db.exists("Course Lesson", lesson.name))
		self.assertFalse(frappe.db.exists("LMS Enrollment", enrollment.name))
		self.assertFalse(frappe.db.exists("LMS Course Progress", {"course": course.name}))
		self.assertFalse(frappe.db.exists("Chapter Reference", {"parent": course.name}))
		self.assertFalse(frappe.db.exists("Lesson Reference", {"parent": chapter.name}))

		# remove from cleanup_items list since delete_course already deleted them
		self.cleanup_items.remove(("LMS Course", course.name))
		self.cleanup_items.remove(("LMS Enrollment", enrollment.name))
		self.cleanup_items.remove(("LMS Course Progress", progress.name))
		self.cleanup_items.remove(("Chapter Reference", chapter_ref.name))
		self.cleanup_items.remove(("Lesson Reference", lesson_ref.name))
		self.cleanup_items.remove(("Course Chapter", chapter.name))
		self.cleanup_items.remove(("Course Lesson", lesson.name))

	def test_delete_course_preserves_certificate_and_detaches_quiz(self):
		"""Regression: deleting a course must preserve the learner's certificate
		(detached, with the frozen course_title snapshot), preserve the embedded
		quiz as a standalone reusable object (both lesson + course back-links
		nulled), and delete LMS Video Watch Duration rows (lesson link is reqd)."""
		course = self._create_course(f"Test DCRC {frappe.generate_hash()}")
		chapter = self._create_chapter(f"Test Chapter {frappe.generate_hash()}", course.name)
		lesson = self._create_lesson(f"Test Lesson {frappe.generate_hash()}", chapter.name, course.name)
		lesson_ref = self._create_lesson_reference(chapter.name, lesson.name)
		chapter_ref = self._create_chapter_reference(course.name, chapter.name)

		user_email = f"test_{frappe.generate_hash()}@example.com"
		self._create_user(user_email, "Test", "Member", ["LMS Student"])
		enrollment = self._create_enrollment(user_email, course.name)

		# Quiz "embedded" in the lesson — mimic save_lesson_details_in_quiz by
		# stamping both back-pointers (course + lesson) onto the quiz.
		self.questions = self._create_quiz_questions()
		quiz = self._create_quiz(f"Test Quiz {frappe.generate_hash()}")
		frappe.db.set_value("LMS Quiz", quiz.name, {"course": course.name, "lesson": lesson.name})

		# Certificate issued to the learner for the course (real validate path).
		# enable_certification + enrollment are already set above. Simulate
		# completion so validate_certification_eligibility passes.
		course.reload()
		course.enable_certification = 1
		course.save()
		progress = frappe.new_doc("LMS Course Progress")
		progress.update(
			{"member": user_email, "course": course.name, "lesson": lesson.name, "status": "Complete"}
		)
		progress.insert()
		self.cleanup_items.append(("LMS Course Progress", progress.name))
		# Force enrollment.progress to 100 so validate_certification_eligibility
		# passes — the recalc-on-Course-Progress hook is flaky in test contexts.
		frappe.db.set_value("LMS Enrollment", enrollment.name, "progress", 100)
		cert = self._create_certificate(course.name, user_email)
		cert_name = cert.name

		# Video-watch row linked to the lesson (lesson is reqd → must be deleted, not detached).
		vw = frappe.get_doc(
			{
				"doctype": "LMS Video Watch Duration",
				"member": user_email,
				"course": course.name,
				"lesson": lesson.name,
				"source": "Youtube",
				"watch_time": 0,
			}
		)
		vw.insert()
		vw_name = vw.name
		self.cleanup_items.append(("LMS Video Watch Duration", vw_name))

		delete_course(course.name)

		# Course-scoped structure gone.
		self.assertFalse(frappe.db.exists("LMS Course", course.name))
		self.assertFalse(frappe.db.exists("Course Chapter", chapter.name))
		self.assertFalse(frappe.db.exists("Course Lesson", lesson.name))
		self.assertFalse(frappe.db.exists("LMS Enrollment", enrollment.name))
		self.assertFalse(frappe.db.exists("LMS Video Watch Duration", vw_name))

		# Certificate preserved + detached. course_title (fetch_from) is the
		# frozen snapshot that survives — that's what the print format renders.
		self.assertTrue(frappe.db.exists("LMS Certificate", cert_name))
		self.assertIsNone(frappe.db.get_value("LMS Certificate", cert_name, "course"))

		# Quiz preserved + fully detached (both course + lesson nulled) so the
		# quiz remains a standalone reusable doc and the lesson delete couldn't
		# be blocked by quiz.lesson.
		self.assertTrue(frappe.db.exists("LMS Quiz", quiz.name))
		self.assertIsNone(frappe.db.get_value("LMS Quiz", quiz.name, "course"))
		self.assertIsNone(frappe.db.get_value("LMS Quiz", quiz.name, "lesson"))

		# remove cleanup entries for things delete_course already deleted
		self.cleanup_items.remove(("LMS Course", course.name))
		self.cleanup_items.remove(("LMS Enrollment", enrollment.name))
		self.cleanup_items.remove(("LMS Course Progress", progress.name))
		self.cleanup_items.remove(("Chapter Reference", chapter_ref.name))
		self.cleanup_items.remove(("Lesson Reference", lesson_ref.name))
		self.cleanup_items.remove(("Course Chapter", chapter.name))
		self.cleanup_items.remove(("Course Lesson", lesson.name))
		self.cleanup_items.remove(("LMS Video Watch Duration", vw_name))
