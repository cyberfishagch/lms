import frappe
from frappe.desk.page.setup_wizard.setup_wizard import add_all_roles_to

from lms.lms.api import give_discussions_permission


def after_install():
	create_batch_source()
	give_discussions_permission()
	give_user_list_permission()


def after_sync():
	create_lms_roles()
	set_default_certificate_print_format()
	give_lms_roles_to_admin()


def after_migrate():
	# `create_lms_roles()` runs in `after_sync` (per-migrate too), but the
	# desk_access=0 invariant has been observed to drift on long-lived
	# benches AFTER setup — Frappe's User.set_system_user() then auto-flips
	# every learner to user_type=System User and they silently disappear
	# from admin enrollment UIs. Re-asserting the invariant on every
	# migrate, AND re-saving affected users so set_system_user() flips them
	# back to Website User, makes drift self-healing at the next deploy.
	enforce_lms_roles_desk_access()


def before_uninstall():
	delete_custom_fields()
	delete_lms_roles()


def create_lms_roles():
	create_course_creator_role()
	create_moderator_role()
	create_evaluator_role()
	create_lms_student_role()


def create_course_creator_role():
	if frappe.db.exists("Role", "Course Creator"):
		frappe.db.set_value("Role", "Course Creator", "desk_access", 0)
	else:
		role = frappe.get_doc(
			{
				"doctype": "Role",
				"role_name": "Course Creator",
				"home_page": "",
				"desk_access": 0,
			}
		)
		role.save()


def create_moderator_role():
	if frappe.db.exists("Role", "Moderator"):
		frappe.db.set_value("Role", "Moderator", "desk_access", 0)
	else:
		role = frappe.get_doc(
			{
				"doctype": "Role",
				"role_name": "Moderator",
				"home_page": "",
				"desk_access": 0,
			}
		)
		role.save()


def create_evaluator_role():
	if frappe.db.exists("Role", "Batch Evaluator"):
		frappe.db.set_value("Role", "Batch Evaluator", "desk_access", 0)
	else:
		role = frappe.new_doc("Role")
		role.update(
			{
				"role_name": "Batch Evaluator",
				"home_page": "",
				"desk_access": 0,
			}
		)
		role.save()


def create_lms_student_role():
	if frappe.db.exists("Role", "LMS Student"):
		frappe.db.set_value("Role", "LMS Student", "desk_access", 0)
	else:
		role = frappe.new_doc("Role")
		role.update(
			{
				"role_name": "LMS Student",
				"home_page": "",
				"desk_access": 0,
			}
		)
		role.save()


# Roles whose `desk_access` flag MUST stay 0. Frappe's `User.set_system_user()`
# auto-promotes any user with a desk-access-bearing role to System User on
# every save — if `desk_access` drifts to 1 on any of these, every learner
# silently flips and disappears from admin enrollment UIs that filter on
# user_type. The Role's default in Frappe core is 1, so any code path that
# (re-)creates these roles without explicit `desk_access=0` re-introduces
# the bug. The two patterns we've seen on prod benches are:
#   - the role getting recreated by a manual desk edit
#   - a fresh migrate that hits a window where set_value short-circuits
# Either way, the fix is the same: reset to 0 and re-save affected users.
LMS_LEARNER_ROLES = ("Course Creator", "Moderator", "Batch Evaluator", "LMS Student")

# Cap on the number of users we'll re-save inside the hook. Re-saving runs
# User.save() per user, which is cheap individually but adds up — and we
# don't want a single `bench migrate` to stall for minutes on a deployment
# with thousands of drifted learners. Above the cap we still reset the
# role flag (the structural fix), and log loudly so an admin can run a
# one-off bulk re-save manually.
LMS_DESK_ACCESS_REPROMOTE_CAP = 200


def enforce_lms_roles_desk_access():
	"""Reset desk_access=0 on the LMS roles and re-save any users whose
	user_type actually flipped to System User as a result of the drift.

	Loud on drift (frappe logger warning) so the issue is visible in bench
	logs rather than hidden. Idempotent — if nothing's drifted, it's a
	cheap series of equality checks.
	"""
	drifted_roles = []
	for role_name in LMS_LEARNER_ROLES:
		if not frappe.db.exists("Role", role_name):
			continue
		current = frappe.db.get_value("Role", role_name, "desk_access")
		if current:
			frappe.db.set_value("Role", role_name, "desk_access", 0)
			drifted_roles.append(role_name)

	if not drifted_roles:
		return

	# Only re-save users whose user_type is *currently wrong*. Learners
	# who weren't promoted don't need a save (saves users from spurious
	# `modified` bumps and audit-log noise) and any non-LMS roles they
	# hold legitimately keeping them as System User stay untouched.
	affected_users = frappe.get_all(
		"User",
		filters=[
			["Has Role", "role", "in", drifted_roles],
			["user_type", "=", "System User"],
			["enabled", "=", 1],
		],
		pluck="name",
	)

	if len(affected_users) > LMS_DESK_ACCESS_REPROMOTE_CAP:
		# Stop the migrate-time loop from running away on a large bench.
		# Role flag is already corrected (the structural part of the fix);
		# the per-user user_type can be re-evaluated by a separate bench
		# command or repeat migrate once the operator confirms.
		frappe.logger().warning(
			f"lms: desk_access drift detected on roles {drifted_roles}; "
			f"reset to 0. {len(affected_users)} users need re-save to flip "
			f"user_type — exceeds cap {LMS_DESK_ACCESS_REPROMOTE_CAP}, skipping "
			"the user-save loop. Run "
			"`bench --site <site> execute lms.install.enforce_lms_roles_desk_access` "
			"manually after deploy to flush the backlog."
		)
		return

	frappe.logger().warning(
		f"lms: desk_access drift detected on roles {drifted_roles}; "
		f"reset to 0. Re-saving {len(affected_users)} affected user(s) to flip user_type."
	)

	# Re-saving runs User.set_system_user() which evaluates the role set
	# against the now-correct desk_access flags and lands user_type on
	# Website User (assuming no OTHER desk-access role on the user).
	for user_name in affected_users:
		try:
			user_doc = frappe.get_doc("User", user_name)
			user_doc.save(ignore_permissions=True)
		except Exception:
			frappe.logger().exception(
				f"lms: failed to re-save user {user_name} during desk_access drift recovery"
			)


def set_default_certificate_print_format():
	filters = {
		"doc_type": "LMS Certificate",
		"property": "default_print_format",
	}
	if not frappe.db.exists("Property Setter", filters):
		filters.update(
			{
				"doctype_or_field": "DocType",
				"property_type": "Data",
				"value": "Certificate",
			}
		)

		doc = frappe.new_doc("Property Setter")
		doc.update(filters)
		doc.save()


def delete_custom_fields():
	fields = [
		"user_category",
		"headline",
		"college",
		"city",
		"verify_terms",
		"country",
		"preferred_location",
		"preferred_functions",
		"preferred_industries",
		"work_environment_column",
		"time",
		"role",
		"carrer_preference_details",
		"skill",
		"certification_details",
		"internship",
		"branch",
		"github",
		"medium",
		"linkedin",
		"profession",
		"open_to",
		"cover_image" "work_environment",
		"dream_companies",
		"career_preference_column",
		"attire",
		"collaboration",
		"location_preference",
		"company_type",
		"skill_details",
		"certification",
		"education",
		"work_experience",
		"education_details",
		"hide_private",
		"work_experience_details",
		"profile_complete",
	]

	for field in fields:
		frappe.db.delete("Custom Field", {"fieldname": field})


def create_batch_source():
	sources = [
		"Newsletter",
		"LinkedIn",
		"Twitter",
		"Website",
		"Friend/Colleague/Connection",
		"Google Search",
	]

	for source in sources:
		if not frappe.db.exists("LMS Source", source):
			doc = frappe.new_doc("LMS Source")
			doc.source = source
			doc.save()


def give_lms_roles_to_admin():
	roles = ["Course Creator", "Moderator", "Batch Evaluator"]
	for role in roles:
		if not frappe.db.exists("Has Role", {"parent": "Administrator", "role": role}):
			doc = frappe.new_doc("Has Role")
			doc.parent = "Administrator"
			doc.parenttype = "User"
			doc.parentfield = "roles"
			doc.role = role
			doc.save()


def give_user_list_permission():
	doctype = "User"
	roles = ["Course Creator", "Moderator", "Batch Evaluator"]
	for role in roles:
		permlevel = 0
		create_role(doctype, role, permlevel)
	create_role(doctype, "System Manager", 1)


def create_role(doctype, role, permlevel):
	if not frappe.db.exists("Custom DocPerm", {"parent": doctype, "role": role, "permlevel": permlevel}):
		doc = frappe.new_doc("Custom DocPerm")
		doc.update(
			{
				"doctype": "Custom DocPerm",
				"parent": doctype,
				"role": role,
				"read": 1,
				"write": 1 if role in ["Moderator", "System Manager"] else 0,
				"create": 1 if role == "Moderator" else 0,
				"permlevel": permlevel,
			}
		)
		doc.save()


def delete_lms_roles():
	roles = ["Course Creator", "Moderator", "Batch Evaluator", "LMS Student"]
	for role in roles:
		if frappe.db.exists("Role", role):
			frappe.db.delete("Role", role)
