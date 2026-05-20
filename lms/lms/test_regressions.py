import frappe
from frappe.tests.utils import FrappeTestCase

from lms.lms.api import clone_course, reset_user_course_progress


class TestCourseCloneAndProgressRegressions(FrappeTestCase):
	def setUp(self):
		super().setUp()
		self.cleanup_items = []
		frappe.set_user("Administrator")

	def tearDown(self):
		frappe.set_user("Administrator")
		for doctype, name in reversed(self.cleanup_items):
			if frappe.db.exists(doctype, name):
				try:
					frappe.delete_doc(doctype, name, force=True, ignore_permissions=True)
				except Exception:
					# Keep teardown best-effort so the real test failure stays visible.
					pass
		super().tearDown()

	def test_clone_course_reuses_quizzes_and_bypasses_chapter_lesson_autoname(self):
		source = self._create_course(
			title=f"Clone Regression Source {frappe.generate_hash(length=8)}",
			instructor="Administrator",
		)
		quiz = self._create_quiz_without_questions()

		# Shared titles reproduce the shape that exposed Frappe's
		# format:{####} autoname collision in clone_course.
		chapter_1 = self._insert_chapter(source.name, "Shared Chapter")
		chapter_2 = self._insert_chapter(source.name, "Shared Chapter")
		self._insert_lesson(source.name, chapter_1.name, "Shared Lesson", quiz.name)
		self._insert_lesson(source.name, chapter_2.name, "Shared Lesson", quiz.name)

		quiz_count_before = frappe.db.count("LMS Quiz")
		first_clone = clone_course(source.name)
		second_clone = clone_course(source.name)

		for result in (first_clone, second_clone):
			self.assertEqual(result["status"], "ok")
			self.assertEqual(result["counts"], {"chapters": 2, "lessons": 2})
			self._track_course_tree(result["name"])

			cloned_chapters = frappe.get_all(
				"Course Chapter",
				filters={"course": result["name"]},
				fields=["name", "title"],
				order_by="idx asc",
			)
			self.assertEqual(len(cloned_chapters), 2)
			for chapter in cloned_chapters:
				self.assertTrue(chapter.name.startswith("clone-"))
				self.assertEqual(chapter.title, "Shared Chapter")

			cloned_lessons = frappe.get_all(
				"Course Lesson",
				filters={"course": result["name"]},
				fields=["name", "title", "quiz_id"],
				order_by="idx asc",
			)
			self.assertEqual(len(cloned_lessons), 2)
			for lesson in cloned_lessons:
				self.assertTrue(lesson.name.startswith("clone-"))
				self.assertEqual(lesson.title, "Shared Lesson")
				self.assertEqual(lesson.quiz_id, quiz.name)

		self.assertEqual(frappe.db.count("LMS Quiz"), quiz_count_before)

	def test_reset_user_course_progress_sweeps_only_course_and_untagged_reusable_quiz_submissions(self):
		member = self._create_user(
			f"reset-regression-{frappe.generate_hash(length=8)}@example.com",
			"Reset",
			"Regression",
			["LMS Student"],
		)
		quiz = self._create_quiz_without_questions()
		course = self._create_course(
			title=f"Reset Regression Course {frappe.generate_hash(length=8)}",
			instructor="Administrator",
		)
		other_course = self._create_course(
			title=f"Reset Regression Other {frappe.generate_hash(length=8)}",
			instructor="Administrator",
		)

		chapter = self._insert_chapter(course.name, "Reset Chapter")
		lesson = self._insert_lesson(course.name, chapter.name, "Reset Lesson", quiz.name)
		other_chapter = self._insert_chapter(other_course.name, "Other Reset Chapter")
		self._insert_lesson(other_course.name, other_chapter.name, "Other Reset Lesson", quiz.name)

		enrollment = self._create_enrollment(member.name, course.name)
		frappe.db.set_value(
			"LMS Enrollment",
			enrollment.name,
			{"progress": 100, "current_lesson": lesson.name},
		)
		self._create_progress(member.name, course.name, lesson.name)

		course_submission = self._insert_quiz_submission(member.name, quiz.name, course.name)
		untagged_submission = self._insert_quiz_submission(member.name, quiz.name, None)
		other_course_submission = self._insert_quiz_submission(member.name, quiz.name, other_course.name)

		result = reset_user_course_progress(course.name, member.name)

		self.assertEqual(result["status"], "ok")
		self.assertFalse(frappe.db.exists("LMS Quiz Submission", course_submission.name))
		self.assertFalse(frappe.db.exists("LMS Quiz Submission", untagged_submission.name))
		self.assertTrue(frappe.db.exists("LMS Quiz Submission", other_course_submission.name))
		self.assertFalse(
			frappe.db.exists(
				"LMS Course Progress",
				{"course": course.name, "member": member.name},
			)
		)

		enrollment.reload()
		self.assertEqual(enrollment.progress, 0)
		self.assertIsNone(enrollment.current_lesson)

	def _create_quiz_without_questions(self):
		quiz = frappe.get_doc(
			{
				"doctype": "LMS Quiz",
				"title": f"Reusable Quiz {frappe.generate_hash(length=8)}",
				"passing_percentage": 70,
				"total_marks": 10,
			}
		)
		quiz.insert(ignore_permissions=True)
		self.cleanup_items.append(("LMS Quiz", quiz.name))
		return quiz

	def _create_user(self, email, first_name, last_name, roles):
		user = frappe.get_doc(
			{
				"doctype": "User",
				"email": email,
				"first_name": first_name,
				"last_name": last_name,
				"user_type": "Website User",
				"send_welcome_email": 0,
				"roles": [{"role": role} for role in roles],
			}
		)
		user.insert(ignore_permissions=True)
		self.cleanup_items.append(("User", user.name))
		return user

	def _create_course(self, title, instructor):
		course = frappe.get_doc(
			{
				"doctype": "LMS Course",
				"title": title,
				"short_introduction": "Regression test course",
				"description": "Regression test course",
				"published": 1,
				"instructors": [{"instructor": instructor}],
			}
		)
		course.insert(ignore_permissions=True)
		self.cleanup_items.append(("LMS Course", course.name))
		return course

	def _create_enrollment(self, member, course):
		enrollment = frappe.get_doc(
			{
				"doctype": "LMS Enrollment",
				"member": member,
				"course": course,
			}
		)
		enrollment.insert(ignore_permissions=True)
		self.cleanup_items.append(("LMS Enrollment", enrollment.name))
		return enrollment

	def _create_progress(self, member, course, lesson):
		progress = frappe.get_doc(
			{
				"doctype": "LMS Course Progress",
				"member": member,
				"course": course,
				"lesson": lesson,
				"status": "Complete",
			}
		)
		progress.insert(ignore_permissions=True)
		self.cleanup_items.append(("LMS Course Progress", progress.name))
		return progress

	def _insert_chapter(self, course, title):
		chapter = frappe.get_doc(
			{
				"doctype": "Course Chapter",
				"course": course,
				"title": title,
			}
		)
		chapter.insert(ignore_permissions=True)
		self.cleanup_items.append(("Course Chapter", chapter.name))
		return chapter

	def _insert_lesson(self, course, chapter, title, quiz_id):
		lesson = frappe.get_doc(
			{
				"doctype": "Course Lesson",
				"course": course,
				"chapter": chapter,
				"title": title,
				"quiz_id": quiz_id,
				"content": '{"blocks":[]}',
			}
		)
		lesson.insert(ignore_permissions=True)
		self.cleanup_items.append(("Course Lesson", lesson.name))
		return lesson

	def _insert_quiz_submission(self, member, quiz, course):
		submission = frappe.get_doc(
			{
				"doctype": "LMS Quiz Submission",
				"quiz": quiz,
				"member": member,
				"course": course,
				"score": 10,
				"score_out_of": 10,
				"percentage": 100,
				"passing_percentage": 70,
			}
		)
		submission.insert(ignore_permissions=True)
		frappe.db.set_value("LMS Quiz Submission", submission.name, "course", course)
		self.cleanup_items.append(("LMS Quiz Submission", submission.name))
		return submission

	def _track_course_tree(self, course):
		self.cleanup_items.append(("LMS Course", course))
		for chapter in frappe.get_all("Course Chapter", {"course": course}, pluck="name"):
			self.cleanup_items.append(("Course Chapter", chapter))
		for lesson in frappe.get_all("Course Lesson", {"course": course}, pluck="name"):
			self.cleanup_items.append(("Course Lesson", lesson))
