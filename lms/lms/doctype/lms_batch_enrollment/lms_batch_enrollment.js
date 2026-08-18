// Copyright (c) 2025, Frappe and contributors
// For license information, please see license.txt

frappe.ui.form.on("LMS Batch Enrollment", {
	refresh(frm) {
		if (!frm.doc.confirmation_email_sent) {
			frm.add_custom_button(__("Send Confirmation Email"), function () {
				frappe.call({
					method: "lms.lms.doctype.lms_batch_enrollment.lms_batch_enrollment.send_confirmation_email",
					args: {
						name: frm.doc.name,
					},
					callback: function (r) {
						frm.refresh();
					},
				});
			});
		}
	},
});
