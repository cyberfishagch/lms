"""Backfill missing Chapter Reference / Lesson Reference rows and recover progress data.

The LMS progress percentage formula in get_course_progress divides the count of
LMS Course Progress(status=Complete) rows by the count of Lesson Reference rows
under Chapter Reference rows under the course. The references are istable child
docs, populated only when their parent (LMS Course / Course Chapter) is saved
with the correct child list. Course Lessons / Course Chapters created (or
reparented) via a path that bypasses the parent save end up as orphans:

  * the Lesson exists in tabCourse Lesson with a chapter link
  * but no Lesson Reference row sits under that chapter
  * so get_lessons() under-counts the denominator
  * and enrollment.progress shows a wrong percentage

This patch:
  1. Inserts missing Chapter Reference rows for every (Course Chapter, course).
  2. Inserts missing Lesson Reference rows for every (Course Lesson, chapter).
  3. Deletes orphan LMS Course Progress rows whose lesson no longer exists.
  4. Dedupes (member, lesson) Course Progress rows, keeping the oldest.
  5. Recalculates enrollment.progress for every LMS Enrollment.

Idempotent: safe to re-run.
"""

import frappe

from lms.lms.utils import recalculate_course_progress


def execute():
	frappe.flags.in_patch = True

	# Order matters: clean stale refs first so backfill doesn't run alongside
	# orphans, then backfill, then clean up Course Progress, then recalc.
	delete_stale_references()
	backfill_chapter_references()
	backfill_lesson_references()
	delete_orphan_progress_rows()
	dedupe_progress_rows()
	recalculate_all_enrollments()


def delete_stale_references():
	"""Remove Lesson/Chapter Reference rows whose target's Link field no longer
	matches the reference's parent (e.g., a lesson was reparented to a different
	chapter, leaving the old Lesson Reference behind). Without this, backfill
	leaves both rows and the denominator double-counts."""

	stale_lesson_refs = frappe.db.sql(
		"""
		SELECT lr.name FROM `tabLesson Reference` lr
		JOIN `tabCourse Lesson` cl ON cl.name = lr.lesson
		WHERE lr.parenttype = 'Course Chapter'
			AND lr.parentfield = 'lessons'
			AND cl.chapter IS NOT NULL
			AND cl.chapter != ''
			AND cl.chapter != lr.parent
		""",
		as_dict=True,
	)
	if stale_lesson_refs:
		names = [r.name for r in stale_lesson_refs]
		print(f"[backfill_progress_references] deleting {len(names)} stale Lesson Reference rows")
		frappe.db.delete("Lesson Reference", {"name": ("in", names)})

	stale_chapter_refs = frappe.db.sql(
		"""
		SELECT cr.name FROM `tabChapter Reference` cr
		JOIN `tabCourse Chapter` cc ON cc.name = cr.chapter
		WHERE cr.parenttype = 'LMS Course'
			AND cr.parentfield = 'chapters'
			AND cc.course IS NOT NULL
			AND cc.course != ''
			AND cc.course != cr.parent
		""",
		as_dict=True,
	)
	if stale_chapter_refs:
		names = [r.name for r in stale_chapter_refs]
		print(f"[backfill_progress_references] deleting {len(names)} stale Chapter Reference rows")
		frappe.db.delete("Chapter Reference", {"name": ("in", names)})


def backfill_chapter_references():
	"""Insert Chapter Reference for any Course Chapter whose course has no matching ref."""
	missing = frappe.db.sql(
		"""
		SELECT cc.name AS chapter, cc.course
		FROM `tabCourse Chapter` cc
		LEFT JOIN `tabChapter Reference` cr
			ON cr.chapter = cc.name
			AND cr.parent = cc.course
			AND cr.parenttype = 'LMS Course'
			AND cr.parentfield = 'chapters'
		WHERE cc.course IS NOT NULL AND cc.course != ''
			AND cr.name IS NULL
		""",
		as_dict=True,
	)
	if not missing:
		print("[backfill_progress_references] no missing Chapter Reference rows")
		return

	print(f"[backfill_progress_references] inserting {len(missing)} Chapter Reference rows")
	for row in missing:
		try:
			max_idx = (
				frappe.db.sql(
					"""SELECT COALESCE(MAX(idx), 0) FROM `tabChapter Reference`
					WHERE parent = %s AND parenttype = 'LMS Course' AND parentfield = 'chapters'""",
					row.course,
				)[0][0]
				or 0
			)
			frappe.get_doc(
				{
					"doctype": "Chapter Reference",
					"parent": row.course,
					"parenttype": "LMS Course",
					"parentfield": "chapters",
					"chapter": row.chapter,
					"idx": max_idx + 1,
				}
			).insert(ignore_permissions=True)
		except Exception as exc:
			# Skip individual failures — likely a deleted parent course or
			# a validator on the parent that doesn't allow the link. The
			# remaining rows should still get backfilled.
			print(f"[backfill_progress_references] failed to insert Chapter Reference for ({row.course}, {row.chapter}): {exc}")


def backfill_lesson_references():
	"""Insert Lesson Reference for any Course Lesson whose chapter has no matching ref."""
	missing = frappe.db.sql(
		"""
		SELECT cl.name AS lesson, cl.chapter
		FROM `tabCourse Lesson` cl
		LEFT JOIN `tabLesson Reference` lr
			ON lr.lesson = cl.name
			AND lr.parent = cl.chapter
			AND lr.parenttype = 'Course Chapter'
			AND lr.parentfield = 'lessons'
		WHERE cl.chapter IS NOT NULL AND cl.chapter != ''
			AND lr.name IS NULL
		""",
		as_dict=True,
	)
	if not missing:
		print("[backfill_progress_references] no missing Lesson Reference rows")
		return

	print(f"[backfill_progress_references] inserting {len(missing)} Lesson Reference rows")
	for row in missing:
		try:
			max_idx = (
				frappe.db.sql(
					"""SELECT COALESCE(MAX(idx), 0) FROM `tabLesson Reference`
					WHERE parent = %s AND parenttype = 'Course Chapter' AND parentfield = 'lessons'""",
					row.chapter,
				)[0][0]
				or 0
			)
			frappe.get_doc(
				{
					"doctype": "Lesson Reference",
					"parent": row.chapter,
					"parenttype": "Course Chapter",
					"parentfield": "lessons",
					"lesson": row.lesson,
					"idx": max_idx + 1,
				}
			).insert(ignore_permissions=True)
		except Exception as exc:
			print(f"[backfill_progress_references] failed to insert Lesson Reference for ({row.chapter}, {row.lesson}): {exc}")


def delete_orphan_progress_rows():
	"""Delete LMS Course Progress rows whose lesson no longer exists."""
	orphans = frappe.db.sql(
		"""
		SELECT p.name FROM `tabLMS Course Progress` p
		LEFT JOIN `tabCourse Lesson` l ON p.lesson = l.name
		WHERE l.name IS NULL
		""",
		as_dict=True,
	)
	if not orphans:
		print("[backfill_progress_references] no orphan Course Progress rows")
		return

	names = [r.name for r in orphans]
	print(f"[backfill_progress_references] deleting {len(names)} orphan Course Progress rows")
	frappe.db.delete("LMS Course Progress", {"name": ("in", names)})


def dedupe_progress_rows():
	"""Keep the oldest Complete row per (member, lesson); delete the rest."""
	dupes = frappe.db.sql(
		"""
		SELECT member, lesson, COUNT(*) AS c
		FROM `tabLMS Course Progress`
		WHERE status = 'Complete'
		GROUP BY member, lesson
		HAVING c > 1
		""",
		as_dict=True,
	)
	if not dupes:
		print("[backfill_progress_references] no duplicate Course Progress rows")
		return

	print(f"[backfill_progress_references] deduping {len(dupes)} (member, lesson) groups")
	for d in dupes:
		rows = frappe.db.get_all(
			"LMS Course Progress",
			filters={"member": d.member, "lesson": d.lesson, "status": "Complete"},
			fields=["name", "creation"],
			order_by="creation asc",
		)
		# Keep the oldest, delete the rest
		to_delete = [r.name for r in rows[1:]]
		if to_delete:
			frappe.db.delete("LMS Course Progress", {"name": ("in", to_delete)})


def recalculate_all_enrollments():
	"""Recalculate enrollment.progress for every LMS Enrollment using the (now-clean) data.

	Commits in chunks of 500 to avoid holding a single long transaction over
	tens of thousands of enrollments.
	"""
	enrollments = frappe.db.get_all(
		"LMS Enrollment",
		fields=["name", "course", "member"],
	)
	print(f"[backfill_progress_references] recalculating progress for {len(enrollments)} enrollments")
	chunk_size = 500
	for i, e in enumerate(enrollments):
		try:
			recalculate_course_progress(e.course, e.member)
		except Exception as exc:
			print(f"[backfill_progress_references] failed for enrollment {e.name}: {exc}")
		if (i + 1) % chunk_size == 0:
			frappe.db.commit()
	frappe.db.commit()
