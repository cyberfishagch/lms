import json

import frappe
from frappe.tests.utils import FrappeTestCase

from lms.lms.api import (
	clone_chapter_into_course,
	clone_course,
	clone_lesson_into_chapter,
	clone_quiz,
	reset_user_course_progress,
)
from lms.install import (
	LMS_DESK_ACCESS_REPROMOTE_CAP,
	LMS_LEARNER_ROLES,
	enforce_lms_roles_desk_access,
)
from lms.lms.doctype.lms_quiz.lms_quiz import quiz_summary


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

	def test_clone_quiz_recovers_when_lms_question_series_counter_has_drifted(self):
		# Reproduces the production error "LMS Question QTS-2026-00109 already
		# exists". `format:QTS-{YYYY}-{#####}` autoname uses Frappe's global
		# Series('') counter (see frappe/tests/test_naming.py:103), which can
		# drift below the real max existing name when records get inserted by
		# fixtures, migrations, or sibling format-autoname doctypes. The next
		# autoname-driven insert in clone_quiz then collides.
		from frappe.utils import now_datetime

		source_quiz = self._create_quiz_with_questions()
		year = now_datetime().strftime("%Y")
		prefix = f"QTS-{year}-"

		# Find the lowest existing LMS Question suffix for this prefix and
		# wind the series counter back to just before it — the next autoname
		# will then land on an already-taken name.
		rows = frappe.db.sql(
			"""SELECT MIN(CAST(SUBSTRING(name, %s) AS UNSIGNED))
			   FROM `tabLMS Question` WHERE name LIKE %s""",
			(len(prefix) + 1, prefix + "%"),
		)
		min_existing = (rows[0][0] if rows and rows[0] else None) or 1
		target_current = max(0, min_existing - 1)

		# tabSeries has no `modified` column, so we use raw SQL — frappe.db
		# helpers add ORDER BY modified / SET modified automatically.
		original_current = frappe.db.get_value("Series", "", "current", order_by="name")
		series_existed = original_current is not None
		if series_existed:
			frappe.db.sql(
				"UPDATE `tabSeries` SET `current` = %s WHERE `name` = ''",
				(target_current,),
			)
		else:
			frappe.db.sql(
				"INSERT INTO `tabSeries` (`name`, `current`) VALUES ('', %s)",
				(target_current,),
			)
		frappe.db.commit()

		try:
			result = clone_quiz(source_quiz.name)
		finally:
			# Restore series state so the rest of the suite isn't affected.
			if series_existed:
				frappe.db.sql(
					"UPDATE `tabSeries` SET `current` = %s WHERE `name` = ''",
					(original_current,),
				)
			else:
				frappe.db.sql("DELETE FROM `tabSeries` WHERE `name` = ''")
			frappe.db.commit()

		cloned_quiz = frappe.get_doc("LMS Quiz", result["name"])
		self._track_quiz_with_questions(cloned_quiz.name)

		self.assertEqual(len(cloned_quiz.questions), 2)
		cloned_question_names = [row.question for row in cloned_quiz.questions]
		self.assertEqual(len(set(cloned_question_names)), 2)
		for qname in cloned_question_names:
			self.assertTrue(qname.startswith(prefix))
			self.assertTrue(frappe.db.exists("LMS Question", qname))

	def test_lms_question_insert_recovers_when_global_series_counter_drifted(self):
		# Reproduces the wizard-side failure: Step 3 of the course wizard calls
		# createDoc('LMS Question') directly (no clone_quiz wrapper), so the
		# retry helper in api.py never fires. The format:QTS-{YYYY}-{#####}
		# autoname uses Frappe's *global* Series('') counter, which drifts
		# below the real max existing name whenever fixtures or sibling
		# format-autoname doctypes increment it. LMSQuestion.before_insert
		# walks the counter forward so every insert path (wizard, admin UI,
		# fixtures) is safe — not just clone_quiz.
		from frappe.utils import now_datetime

		year = now_datetime().strftime("%Y")
		prefix = f"QTS-{year}-"

		# Make sure at least one QTS-{year}-* row exists so there's a max to
		# sync TO. Otherwise the first ever insert in a fresh DB has nothing
		# to recover from and the test is vacuous.
		anchor = frappe.get_doc({
			"doctype": "LMS Question",
			"question": f"Anchor {frappe.generate_hash(length=6)}",
			"type": "Choices",
			"option_1": "A",
			"option_2": "B",
			"is_correct_1": 1,
		})
		anchor.insert(ignore_permissions=True)
		self.cleanup_items.append(("LMS Question", anchor.name))

		rows = frappe.db.sql(
			"""SELECT MAX(CAST(SUBSTRING(name, %s) AS UNSIGNED))
			   FROM `tabLMS Question` WHERE name LIKE %s""",
			(len(prefix) + 1, prefix + "%"),
		)
		max_existing = (rows[0][0] if rows and rows[0] else None) or 0

		# Desync: push Series('') back so the next autoname would land on an
		# already-used name. Raw SQL — tabSeries has no `modified` column.
		original = frappe.db.get_value("Series", "", "current", order_by="name")
		series_existed = original is not None
		target = max(0, max_existing - 3)
		if series_existed:
			frappe.db.sql(
				"UPDATE `tabSeries` SET `current` = %s WHERE `name` = ''",
				(target,),
			)
		else:
			frappe.db.sql(
				"INSERT INTO `tabSeries` (`name`, `current`) VALUES ('', %s)",
				(target,),
			)
		frappe.db.commit()

		try:
			# This insert hits before_insert which re-syncs Series('') to
			# max_existing, so autoname produces max_existing+1 cleanly.
			fresh = frappe.get_doc({
				"doctype": "LMS Question",
				"question": f"Fresh {frappe.generate_hash(length=6)}",
				"type": "Choices",
				"option_1": "A",
				"option_2": "B",
				"is_correct_1": 1,
			})
			fresh.insert(ignore_permissions=True)
			self.cleanup_items.append(("LMS Question", fresh.name))
		finally:
			if series_existed:
				frappe.db.sql(
					"UPDATE `tabSeries` SET `current` = %s WHERE `name` = ''",
					(original,),
				)
			else:
				frappe.db.sql("DELETE FROM `tabSeries` WHERE `name` = ''")
			frappe.db.commit()

		self.assertTrue(fresh.name.startswith(prefix))
		fresh_suffix = int(fresh.name[len(prefix):])
		# The new question must come AFTER every previously-existing row.
		self.assertGreater(fresh_suffix, max_existing)

	def test_quiz_summary_stamps_course_on_submission_for_cross_course_scoping(self):
		# Reproduces the cross-course skip regression: prior to this fix,
		# create_submission saved every LMS Quiz Submission with course=NULL.
		# The same LMS Quiz is reused across courses (lms#14), so a pass in
		# course A made LessonPage's quizPassed gate true in course B and
		# learners could skip the quiz-after-video gate. Now quiz_summary
		# accepts course and stamps it on the row.
		import json

		course = self._create_course(
			title=f"Quiz Attribution Course {frappe.generate_hash(length=8)}",
			instructor="Administrator",
		)
		other_course = self._create_course(
			title=f"Quiz Attribution Other {frappe.generate_hash(length=8)}",
			instructor="Administrator",
		)
		quiz = self._create_quiz_with_questions()
		# Use a high passing_percentage so save_progress_after_quiz neither
		# triggers the >= branch (we won't score that high with fake answers)
		# nor the "no passing threshold" elif branch — both call save_progress
		# which requires lesson + course on the quiz doc, and we don't link
		# either since the assertion is on the submission row, not progress.
		quiz.passing_percentage = 100
		quiz.save(ignore_permissions=True)

		# Build a non-empty result payload that quiz_summary can process.
		# We only care that a submission row gets written with course set —
		# the scoring outcome isn't under test.
		results = [
			{
				"question_name": row.question,
				"is_correct": [1],
				"answer": "",
			}
			for row in quiz.questions
		]

		response_a = quiz_summary(quiz=quiz.name, results=json.dumps(results), course=course.name)
		self.cleanup_items.append(("LMS Quiz Submission", response_a["submission"]))
		response_b = quiz_summary(quiz=quiz.name, results=json.dumps(results), course=other_course.name)
		self.cleanup_items.append(("LMS Quiz Submission", response_b["submission"]))
		response_untagged = quiz_summary(quiz=quiz.name, results=json.dumps(results))
		self.cleanup_items.append(("LMS Quiz Submission", response_untagged["submission"]))

		sub_a = frappe.get_doc("LMS Quiz Submission", response_a["submission"])
		sub_b = frappe.get_doc("LMS Quiz Submission", response_b["submission"])
		sub_untagged = frappe.get_doc("LMS Quiz Submission", response_untagged["submission"])

		self.assertEqual(sub_a.course, course.name)
		self.assertEqual(sub_b.course, other_course.name)
		# Back-compat: legacy callers without `course` keep submitting untagged.
		self.assertFalse(sub_untagged.course)

	def test_enforce_lms_roles_desk_access_resets_drift_and_repromotes_affected_users(self):
		# Reproduces the prod drift: `LMS Student.desk_access` ended up at 1
		# despite install.py + a migration patch both setting it to 0.
		# Frappe core's User.set_system_user() then auto-flipped every user
		# with that role to user_type=System User, and the admin enrollment
		# combobox silently excluded them.
		#
		# The new after_migrate hook (`enforce_lms_roles_desk_access`) must:
		#  1. Reset desk_access=0 on every LMS role that drifted to 1.
		#  2. Re-save users with the affected role so set_system_user()
		#     re-evaluates them back to Website User.
		# Idempotent — no-op when nothing's drifted.

		# Set up a user with LMS Student role; baseline must be Website User.
		email = f"desk-drift-{frappe.generate_hash(length=8)}@example.com"
		user = self._create_user(email, "Drift", "Test", ["LMS Student"])
		self.assertEqual(frappe.db.get_value("User", email, "user_type"), "Website User")

		# Force the drift: set desk_access=1 on LMS Student, save the user
		# to trigger Frappe's auto-flip to System User. (We don't re-save
		# the role through the doc API — that would cascade and re-evaluate
		# all users with it; we need to simulate the *quiet* drift that
		# left users desync'd from the role's flag.)
		frappe.db.set_value("Role", "LMS Student", "desk_access", 1)
		user_doc = frappe.get_doc("User", email)
		user_doc.save(ignore_permissions=True)
		self.assertEqual(frappe.db.get_value("User", email, "user_type"), "System User")

		# Run the hook — should detect drift, reset, re-save affected users.
		try:
			enforce_lms_roles_desk_access()
		finally:
			# Guarantee teardown leaves the role correct even if the
			# assertion below fails.
			frappe.db.set_value("Role", "LMS Student", "desk_access", 0)

		# Role reset.
		self.assertEqual(frappe.db.get_value("Role", "LMS Student", "desk_access"), 0)
		# User re-saved → user_type back to Website User.
		self.assertEqual(frappe.db.get_value("User", email, "user_type"), "Website User")

	def test_enforce_lms_roles_desk_access_caps_user_resave_loop(self):
		# When too many users would need re-save, the hook MUST still reset
		# the role flag (structural fix) but skip the per-user save loop
		# (bounded runtime). The cap prevents `bench migrate` from stalling
		# for minutes on a large drifted deployment. Monkeypatch the cap
		# down to 1 so we can exercise the path with a single affected user.
		from unittest.mock import patch as mock_patch

		email = f"desk-drift-cap-{frappe.generate_hash(length=8)}@example.com"
		self._create_user(email, "Cap", "Test", ["LMS Student"])

		# Force drift state matching the prod observation.
		frappe.db.set_value("Role", "LMS Student", "desk_access", 1)
		user_doc = frappe.get_doc("User", email)
		user_doc.save(ignore_permissions=True)
		self.assertEqual(frappe.db.get_value("User", email, "user_type"), "System User")

		try:
			with mock_patch("lms.install.LMS_DESK_ACCESS_REPROMOTE_CAP", 0):
				enforce_lms_roles_desk_access()
		finally:
			frappe.db.set_value("Role", "LMS Student", "desk_access", 0)

		# Role reset MUST still happen (structural fix is independent of the cap).
		self.assertEqual(frappe.db.get_value("Role", "LMS Student", "desk_access"), 0)
		# User_type stays System User — re-save loop was skipped by the cap.
		# Operator is responsible for running the hook manually after deploy.
		self.assertEqual(frappe.db.get_value("User", email, "user_type"), "System User")

		# Sanity: the cap default is sane (not 0 or undefined).
		self.assertGreater(LMS_DESK_ACCESS_REPROMOTE_CAP, 0)

	def test_enforce_lms_roles_desk_access_is_noop_when_nothing_drifted(self):
		# Idempotency check: every LMS_LEARNER_ROLES role at desk_access=0
		# means the hook makes zero writes. Snapshot the role modified
		# timestamps; they must not change.
		baseline = {
			role: frappe.db.get_value("Role", role, ["desk_access", "modified"], as_dict=True)
			for role in LMS_LEARNER_ROLES
			if frappe.db.exists("Role", role)
		}
		# Ensure baseline is clean — guard against test ordering side-effects.
		for role in baseline:
			if baseline[role].desk_access:
				frappe.db.set_value("Role", role, "desk_access", 0)
				baseline[role] = frappe.db.get_value(
					"Role", role, ["desk_access", "modified"], as_dict=True
				)

		enforce_lms_roles_desk_access()

		for role, before in baseline.items():
			after = frappe.db.get_value("Role", role, ["desk_access", "modified"], as_dict=True)
			self.assertEqual(after.desk_access, 0, f"{role} desk_access drifted")
			self.assertEqual(
				str(after.modified), str(before.modified), f"{role} was modified unnecessarily"
			)

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

	def test_clone_chapter_into_course_brings_lessons_and_reuses_quizzes(self):
		source = self._create_course(
			title=f"Chapter Clone Source {frappe.generate_hash(length=8)}",
			instructor="Administrator",
		)
		target = self._create_course(
			title=f"Chapter Clone Target {frappe.generate_hash(length=8)}",
			instructor="Administrator",
		)
		chapter = self._insert_chapter(source.name, "Reusable Chapter")
		quiz = self._create_quiz_without_questions()
		quiz_content = json.dumps(
			{"blocks": [{"type": "quiz", "data": {"quiz": quiz.name}}]}
		)
		lesson_1 = self._insert_lesson(source.name, chapter.name, "Quiz ID Lesson", quiz.name)
		lesson_2 = self._insert_lesson(
			source.name,
			chapter.name,
			"Content Quiz Lesson",
			content=quiz_content,
		)
		self._insert_lesson(source.name, chapter.name, "Plain Lesson")

		quiz_count_before = frappe.db.count("LMS Quiz")
		result = clone_chapter_into_course(chapter.name, target.name)
		self.cleanup_items.append(("Course Chapter", result["chapter"]))

		new_chapter = frappe.get_doc("Course Chapter", result["chapter"])
		self.assertEqual(new_chapter.course, target.name)
		self.assertEqual(
			result,
			{"chapter": new_chapter.name, "title": new_chapter.title, "lesson_count": 3},
		)

		new_lessons = frappe.get_all(
			"Course Lesson",
			filters={"chapter": new_chapter.name},
			fields=["name", "title", "idx", "quiz_id", "content"],
			order_by="idx asc",
		)
		for lesson in new_lessons:
			self.cleanup_items.append(("Course Lesson", lesson.name))

		self.assertEqual(len(new_lessons), 3)
		self.assertEqual([lesson.idx for lesson in new_lessons], [1, 2, 3])
		self.assertEqual(
			[lesson.title for lesson in new_lessons],
			["Quiz ID Lesson", "Content Quiz Lesson", "Plain Lesson"],
		)
		self.assertEqual(new_lessons[0].quiz_id, lesson_1.quiz_id)
		self.assertEqual(new_lessons[1].content, lesson_2.content)
		self.assertEqual(frappe.db.count("LMS Quiz"), quiz_count_before)

	def test_clone_chapter_into_course_handles_empty_chapter(self):
		source = self._create_course(
			title=f"Empty Chapter Clone Source {frappe.generate_hash(length=8)}",
			instructor="Administrator",
		)
		target = self._create_course(
			title=f"Empty Chapter Clone Target {frappe.generate_hash(length=8)}",
			instructor="Administrator",
		)
		chapter = self._insert_chapter(source.name, "Empty Reusable Chapter")

		result = clone_chapter_into_course(chapter.name, target.name)
		self.cleanup_items.append(("Course Chapter", result["chapter"]))

		new_chapter = frappe.get_doc("Course Chapter", result["chapter"])
		self.assertEqual(
			result,
			{"chapter": new_chapter.name, "title": new_chapter.title, "lesson_count": 0},
		)
		self.assertEqual(new_chapter.course, target.name)
		self.assertEqual(frappe.db.count("Course Lesson", {"chapter": new_chapter.name}), 0)
		self.assertEqual(len(new_chapter.lessons or []), 0)

	def test_clone_chapter_into_course_rejects_unauthorized(self):
		source = self._create_course(
			title=f"Unauthorized Chapter Clone Source {frappe.generate_hash(length=8)}",
			instructor="Administrator",
		)
		target = self._create_course(
			title=f"Unauthorized Chapter Clone Target {frappe.generate_hash(length=8)}",
			instructor="Administrator",
		)
		chapter = self._insert_chapter(source.name, "Unauthorized Chapter")
		member = self._create_user(
			f"chapter-clone-denied-{frappe.generate_hash(length=8)}@example.com",
			"Chapter",
			"Denied",
			["LMS Student"],
		)

		frappe.set_user(member.name)
		with self.assertRaises(frappe.PermissionError):
			clone_chapter_into_course(chapter.name, target.name)

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
		quiz_id=None,
		content='{"blocks":[]}',
		instructor_content=None,
	):
		idx = frappe.db.count("Course Lesson", {"chapter": chapter}) + 1
		lesson = frappe.get_doc(
			{
				"doctype": "Course Lesson",
				"course": course,
				"chapter": chapter,
				"idx": idx,
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
