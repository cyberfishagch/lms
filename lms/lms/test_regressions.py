import frappe
from frappe.tests.utils import FrappeTestCase

from lms.lms.api import (
	clone_course,
	clone_lesson_into_chapter,
	clone_quiz,
	reset_user_course_progress,
)


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

	def test_clone_lesson_into_chapter_reuses_quizzes(self):
		source_course = self._create_course(
			title=f"Lesson Clone Source {frappe.generate_hash(length=8)}",
			instructor="Administrator",
		)
		target_course = self._create_course(
			title=f"Lesson Clone Target {frappe.generate_hash(length=8)}",
			instructor="Administrator",
		)
		source_chapter = self._insert_chapter(source_course.name, "Source Chapter")
		target_chapter = self._insert_chapter(target_course.name, "Target Chapter")
		quiz = self._create_quiz_without_questions()
		content = f'{{"blocks":[{{"type":"quiz","data":{{"quiz":"{quiz.name}"}}}}]}}'
		instructor_content = (
			f'{{"blocks":[{{"type":"upload","data":{{"quizzes":[{{"quiz":"{quiz.name}"}}]}}}}]}}'
		)
		source_lesson = self._insert_lesson(
			source_course.name,
			source_chapter.name,
			"Reusable Quiz Lesson",
			quiz.name,
			content=content,
			instructor_content=instructor_content,
		)
		quiz_count_before = frappe.db.count("LMS Quiz")

		result = clone_lesson_into_chapter(source_lesson.name, target_chapter.name)
		self.cleanup_items.append(("Course Lesson", result["lesson"]))
		new_lesson = frappe.get_doc("Course Lesson", result["lesson"])

		self.assertNotIn("quizzes", result)
		self.assertEqual(result["title"], source_lesson.title)
		self.assertEqual(new_lesson.quiz_id, quiz.name)
		self.assertEqual(new_lesson.content, content)
		self.assertEqual(new_lesson.instructor_content, instructor_content)
		self.assertEqual(frappe.db.count("LMS Quiz"), quiz_count_before)

	def test_clone_quiz_deep_copies_questions_and_handles_title_collision(self):
		source_quiz = self._create_quiz_with_questions()

		first_result = clone_quiz(source_quiz.name)
		first_clone = frappe.get_doc("LMS Quiz", first_result["name"])
		self._track_quiz_with_questions(first_clone.name)

		self.assertNotEqual(first_clone.name, source_quiz.name)
		self.assertNotEqual(first_clone.title, source_quiz.title)
		self.assertEqual(first_clone.title, f"{source_quiz.title} (copy)")
		self.assertEqual(len(first_clone.questions), 2)

		source_question_names = [row.question for row in source_quiz.questions]
		cloned_question_names = [row.question for row in first_clone.questions]
		self.assertEqual(len(set(cloned_question_names)), 2)
		for question_name in cloned_question_names:
			self.assertNotIn(question_name, source_question_names)
			self.assertTrue(frappe.db.exists("LMS Question", question_name))

		cloned_question = frappe.get_doc("LMS Question", cloned_question_names[0])
		cloned_question.question = "Edited cloned question?"
		cloned_question.save(ignore_permissions=True)
		source_question = frappe.get_doc("LMS Question", source_question_names[0])
		self.assertNotEqual(source_question.question, cloned_question.question)

		second_result = clone_quiz(source_quiz.name)
		second_clone = frappe.get_doc("LMS Quiz", second_result["name"])
		self._track_quiz_with_questions(second_clone.name)

		self.assertNotEqual(second_clone.name, source_quiz.name)
		self.assertNotEqual(second_clone.title, first_clone.title)
		self.assertTrue(second_clone.title.startswith(f"{source_quiz.title} (copy "))

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

	def _create_question(self, index):
		question = frappe.get_doc(
			{
				"doctype": "LMS Question",
				"question": f"Regression question {index}?",
				"type": "Choices",
				"option_1": "Correct",
				"is_correct_1": 1,
				"option_2": "Incorrect",
				"is_correct_2": 0,
			}
		)
		question.insert(ignore_permissions=True)
		self.cleanup_items.append(("LMS Question", question.name))
		return question

	def _create_quiz_with_questions(self):
		questions = [self._create_question(1), self._create_question(2)]
		quiz = frappe.get_doc(
			{
				"doctype": "LMS Quiz",
				"title": f"Deep Copy Quiz {frappe.generate_hash(length=8)}",
				"passing_percentage": 70,
				"questions": [
					{"question": questions[0].name, "marks": 3},
					{"question": questions[1].name, "marks": 4},
				],
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

	def _insert_lesson(
		self,
		course,
		chapter,
		title,
		quiz_id,
		content='{"blocks":[]}',
		instructor_content=None,
	):
		lesson = frappe.get_doc(
			{
				"doctype": "Course Lesson",
				"course": course,
				"chapter": chapter,
				"title": title,
				"quiz_id": quiz_id,
				"content": content,
				"instructor_content": instructor_content,
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

	def _track_quiz_with_questions(self, quiz):
		for question in frappe.get_all("LMS Quiz Question", {"parent": quiz}, pluck="question"):
			self.cleanup_items.append(("LMS Question", question))
		self.cleanup_items.append(("LMS Quiz", quiz))
