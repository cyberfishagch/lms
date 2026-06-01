# Copyright (c) 2021, FOSS United and Contributors
# See license.txt

import frappe

from lms.lms.api import (
	delete_course,
	get_trashed_courses,
	permanently_delete_course,
	restore_course,
)
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

	def test_soft_delete_course(self):
		"""`delete_course` now soft-deletes: course row stays, is_deleted=1,
		published flipped to 0, all linked rows (chapter/lesson/refs/enrollment/
		progress) remain intact. Hidden from `frappe.get_all` via the
		permission_query_conditions hook."""
		course = self._create_course(f"Test Course {frappe.generate_hash()}")
		chapter = self._create_chapter(f"Test Chapter {frappe.generate_hash()}", course.name)
		lesson = self._create_lesson(f"Test Lesson {frappe.generate_hash()}", chapter.name, course.name)
		self._create_lesson_reference(chapter.name, lesson.name)
		self._create_chapter_reference(course.name, chapter.name)

		user_email = f"test_{frappe.generate_hash()}@example.com"
		self._create_user(user_email, "Test", "Member", ["LMS Student"])
		enrollment = self._create_enrollment(user_email, course.name)
		self._create_progress(user_email, course.name, lesson.name)

		delete_course(course.name)

		# Course row is alive but flagged.
		self.assertTrue(frappe.db.exists("LMS Course", course.name))
		fresh = frappe.db.get_value(
			"LMS Course", course.name, ["is_deleted", "deleted_by", "published"], as_dict=True
		)
		self.assertEqual(fresh.is_deleted, 1)
		self.assertEqual(fresh.deleted_by, frappe.session.user)
		self.assertEqual(fresh.published, 0)

		# Every linked row survives — no cascade.
		self.assertTrue(frappe.db.exists("Course Chapter", chapter.name))
		self.assertTrue(frappe.db.exists("Course Lesson", lesson.name))
		self.assertTrue(frappe.db.exists("LMS Enrollment", enrollment.name))
		self.assertTrue(frappe.db.exists("LMS Course Progress", {"course": course.name}))
		self.assertTrue(frappe.db.exists("Chapter Reference", {"parent": course.name}))
		self.assertTrue(frappe.db.exists("Lesson Reference", {"parent": chapter.name}))

		# Hidden from `frappe.get_list` via the permission_query_conditions hook.
		# (Note: `frappe.get_all` bypasses permission_query_conditions — it's
		# `get_list(ignore_permissions=True)`. The frontend's
		# `useFrappeGetDocList` hits `frappe.client.get_list` which applies
		# the hook, so the user-facing path is filtered.)
		listed = frappe.get_list("LMS Course", filters={"name": course.name}, pluck="name")
		self.assertNotIn(course.name, listed)

		# But visible when the Trash flag is set.
		frappe.flags.lms_show_trash = True
		try:
			listed_with_trash = frappe.get_list(
				"LMS Course", filters={"name": course.name}, pluck="name"
			)
		finally:
			frappe.flags.lms_show_trash = False
		self.assertIn(course.name, listed_with_trash)

	def test_restore_course(self):
		"""Restore clears the trash flags and brings the course back into
		default `frappe.get_all` results. `published` stays 0 — restored
		course is a Draft; admin re-publishes deliberately."""
		course = self._create_course(f"Test Restore {frappe.generate_hash()}")

		delete_course(course.name)
		self.assertEqual(frappe.db.get_value("LMS Course", course.name, "is_deleted"), 1)

		restore_course(course.name)
		fresh = frappe.db.get_value(
			"LMS Course", course.name, ["is_deleted", "deleted_on", "deleted_by", "published"], as_dict=True
		)
		self.assertEqual(fresh.is_deleted, 0)
		self.assertIsNone(fresh.deleted_on)
		self.assertIsNone(fresh.deleted_by)
		self.assertEqual(fresh.published, 0)  # restored as Draft

		# Back in default listings.
		listed = frappe.get_list("LMS Course", filters={"name": course.name}, pluck="name")
		self.assertIn(course.name, listed)

	def test_permanently_delete_requires_soft_delete_first(self):
		"""Hard cascade refuses on a live course; passes after soft-delete;
		also passes immediately when called with `force=True`."""
		course = self._create_course(f"Test PermDel Gate {frappe.generate_hash()}")

		# Not yet trashed → must throw.
		with self.assertRaises(frappe.ValidationError):
			permanently_delete_course(course.name)

		delete_course(course.name)
		# Now in Trash → permanent delete allowed.
		permanently_delete_course(course.name)
		self.assertFalse(frappe.db.exists("LMS Course", course.name))

		# cleanup_items has the course because the helper appended; the row is
		# gone now so tearDown will skip via its `exists` check, but we drop
		# the entry to be tidy.
		self.cleanup_items.remove(("LMS Course", course.name))

	def test_permanently_delete_with_force_skips_soft_step(self):
		"""`force=True` bypasses the two-step gate — used by the course
		wizard's rollback path for a partially-created course."""
		course = self._create_course(f"Test PermDel Force {frappe.generate_hash()}")
		permanently_delete_course(course.name, force=True)
		self.assertFalse(frappe.db.exists("LMS Course", course.name))
		self.cleanup_items.remove(("LMS Course", course.name))

	def test_permanently_delete_course_preserves_certificate_and_detaches_quiz(self):
		"""Regression for the shipped hard-delete fix, now exercised through
		`permanently_delete_course` (after a prerequisite soft-delete).

		Asserts: course row + chapter + lesson + enrollment + video-watch
		are GONE; LMS Certificate and LMS Quiz SURVIVE detached (course/lesson
		nulled) so learners keep their credentials and reusable quizzes."""
		course = self._create_course(f"Test PermDel Preserve {frappe.generate_hash()}")
		chapter = self._create_chapter(f"Test Chapter {frappe.generate_hash()}", course.name)
		lesson = self._create_lesson(f"Test Lesson {frappe.generate_hash()}", chapter.name, course.name)
		lesson_ref = self._create_lesson_reference(chapter.name, lesson.name)
		chapter_ref = self._create_chapter_reference(course.name, chapter.name)

		user_email = f"test_{frappe.generate_hash()}@example.com"
		self._create_user(user_email, "Test", "Member", ["LMS Student"])
		enrollment = self._create_enrollment(user_email, course.name)

		self.questions = self._create_quiz_questions()
		quiz = self._create_quiz(f"Test Quiz {frappe.generate_hash()}")
		frappe.db.set_value("LMS Quiz", quiz.name, {"course": course.name, "lesson": lesson.name})

		course.reload()
		course.enable_certification = 1
		course.save()
		progress = frappe.new_doc("LMS Course Progress")
		progress.update(
			{"member": user_email, "course": course.name, "lesson": lesson.name, "status": "Complete"}
		)
		progress.insert()
		self.cleanup_items.append(("LMS Course Progress", progress.name))
		frappe.db.set_value("LMS Enrollment", enrollment.name, "progress", 100)
		cert = self._create_certificate(course.name, user_email)
		cert_name = cert.name

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

		# Two-step: soft-delete then permanent delete.
		delete_course(course.name)
		permanently_delete_course(course.name)

		self.assertFalse(frappe.db.exists("LMS Course", course.name))
		self.assertFalse(frappe.db.exists("Course Chapter", chapter.name))
		self.assertFalse(frappe.db.exists("Course Lesson", lesson.name))
		self.assertFalse(frappe.db.exists("LMS Enrollment", enrollment.name))
		self.assertFalse(frappe.db.exists("LMS Video Watch Duration", vw_name))

		self.assertTrue(frappe.db.exists("LMS Certificate", cert_name))
		self.assertIsNone(frappe.db.get_value("LMS Certificate", cert_name, "course"))

		self.assertTrue(frappe.db.exists("LMS Quiz", quiz.name))
		self.assertIsNone(frappe.db.get_value("LMS Quiz", quiz.name, "course"))
		self.assertIsNone(frappe.db.get_value("LMS Quiz", quiz.name, "lesson"))

		self.cleanup_items.remove(("LMS Course", course.name))
		self.cleanup_items.remove(("LMS Enrollment", enrollment.name))
		self.cleanup_items.remove(("LMS Course Progress", progress.name))
		self.cleanup_items.remove(("Chapter Reference", chapter_ref.name))
		self.cleanup_items.remove(("Lesson Reference", lesson_ref.name))
		self.cleanup_items.remove(("Course Chapter", chapter.name))
		self.cleanup_items.remove(("Course Lesson", lesson.name))
		self.cleanup_items.remove(("LMS Video Watch Duration", vw_name))

	def test_get_trashed_courses_lists_only_deleted(self):
		"""The admin Trash list RPC returns only soft-deleted courses."""
		live = self._create_course(f"Test Live {frappe.generate_hash()}")
		trashed = self._create_course(f"Test Trashed {frappe.generate_hash()}")
		delete_course(trashed.name)

		names = {row.name for row in get_trashed_courses(limit=500)}
		self.assertIn(trashed.name, names)
		self.assertNotIn(live.name, names)

	def test_enrollment_hook_hides_trashed_course_enrollments(self):
		"""Enrollments to a soft-deleted course are hidden by the
		permission_query_conditions hook on LMS Enrollment — `/dashboard` and
		`/my-courses` query Enrollment directly."""
		course = self._create_course(f"Test EnrHook {frappe.generate_hash()}")
		user_email = f"test_{frappe.generate_hash()}@example.com"
		self._create_user(user_email, "Test", "Member", ["LMS Student"])
		enrollment = self._create_enrollment(user_email, course.name)

		# Visible before delete (use get_list — get_all bypasses permission_query_conditions).
		visible = frappe.get_list("LMS Enrollment", filters={"name": enrollment.name}, pluck="name")
		self.assertIn(enrollment.name, visible)

		delete_course(course.name)

		# Hidden after delete via the LMS Enrollment EXISTS-join hook.
		hidden = frappe.get_list("LMS Enrollment", filters={"name": enrollment.name}, pluck="name")
		self.assertNotIn(enrollment.name, hidden)
