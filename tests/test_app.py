import io
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pricing import create_app  # noqa: E402
from pricing import db  # noqa: E402

SAMPLE = """框号,文字,x1,y1,x2,y2
1,白砂糖,10,10,90,40
2,一级,110,10,190,40
3,5.00,210,10,290,40
4,红糖,10,110,90,140
5,1.20,210,110,290,140
6,跨格,90,10,110,40
7,出界,400,400,450,430
"""


class AppTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, "test.sqlite3")
        self.app = create_app({
            "TESTING": True,
            "DATABASE": self.db_path,
            "SECRET_KEY": "test",
        })
        self.client = self.app.test_client()

    def tearDown(self):
        self.tmp.cleanup()

    def upload(self, body=SAMPLE, filename="page.csv"):
        return self.client.post(
            "/upload",
            data={"file": (io.BytesIO(body.encode("utf-8")), filename)},
            content_type="multipart/form-data",
            follow_redirects=False,
        )

    def upload_and_get_job(self):
        response = self.upload()
        self.assertEqual(response.status_code, 302)
        return int(response.headers["Location"].rstrip("/").split("/")[-1])

    def set_lines(self, job_id, horizontal, vertical):
        return self.client.post(
            f"/api/jobs/{job_id}/lines",
            json={"horizontal": horizontal, "vertical": vertical},
        )

    def state(self, job_id):
        response = self.client.get(f"/api/jobs/{job_id}")
        self.assertEqual(response.status_code, 200)
        return response.get_json()

    def box_id(self, state, ref):
        return next(item["box_id"] for item in state["boxes"] if item["box_ref"] == ref)


class UploadTests(AppTestCase):
    def test_upload_creates_job_and_redirects(self):
        job_id = self.upload_and_get_job()
        state = self.state(job_id)
        self.assertEqual(len(state["boxes"]), 7)
        self.assertIn(state["boxes"][0]["status"],
                      ("assigned_auto", "pending", "stale"))

    def test_upload_rejects_empty_file(self):
        response = self.client.post(
            "/upload",
            data={"file": (io.BytesIO(b""), "empty.csv")},
            content_type="multipart/form-data",
            follow_redirects=True,
        )
        self.assertIn("文件为空".encode("utf-8"), response.data)

    def test_upload_without_file_redirects_with_error(self):
        response = self.client.post("/upload", data={}, follow_redirects=True)
        self.assertIn("请选择一个 CSV 文件".encode("utf-8"), response.data)

    def test_job_page_renders(self):
        job_id = self.upload_and_get_job()
        response = self.client.get(f"/jobs/{job_id}")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"grid-lines", response.data)


class LineTests(AppTestCase):
    def test_invalid_lines_keep_previous_layout_and_assignments(self):
        job_id = self.upload_and_get_job()
        self.assertEqual(self.set_lines(job_id, [0, 100, 200], [0, 100, 200, 300])
                         .status_code, 200)
        before = self.state(job_id)
        response = self.set_lines(job_id, [0, 100], [0, 100, 200])
        self.assertEqual(response.status_code, 400)
        self.assertFalse(response.get_json()["ok"])
        after = self.state(job_id)
        self.assertEqual(before["horizontal"], after["horizontal"])
        self.assertEqual(before["vertical"], after["vertical"])
        self.assertEqual([(b["box_id"], b["status"]) for b in before["boxes"]],
                         [(b["box_id"], b["status"]) for b in after["boxes"]])

    def test_stale_pending_decisions_after_boundary_move(self):
        job_id = self.upload_and_get_job()
        self.set_lines(job_id, [0, 100, 200], [0, 100, 200, 300])
        state = self.state(job_id)
        box = self.box_id(state, "6")
        decision = self.client.post(
            f"/api/jobs/{job_id}/decisions",
            json={"box_id": box, "kind": "cell", "target_cell": "100|0|200|100"},
        )
        self.assertEqual(decision.status_code, 200)
        self.assertEqual(self.state(job_id)["status_counts"].get("assigned_manual"), 1)

        self.set_lines(job_id, [0, 100, 200], [0, 95, 200, 300])
        state = self.state(job_id)
        item = next(entry for entry in state["boxes"] if entry["box_id"] == box)
        self.assertEqual(item["status"], "stale")

    def test_decision_survives_unrelated_line_change(self):
        job_id = self.upload_and_get_job()
        self.set_lines(job_id, [0, 100, 200], [0, 100, 200, 300])
        box = self.box_id(self.state(job_id), "6")
        self.client.post(f"/api/jobs/{job_id}/decisions",
                         json={"box_id": box, "kind": "cell",
                               "target_cell": "100|0|200|100"})
        # Moving the bottom border y=200 does not touch box 6's cells.
        self.set_lines(job_id, [0, 100, 260], [0, 100, 200, 300])
        item = next(entry for entry in self.state(job_id)["boxes"]
                    if entry["box_id"] == box)
        self.assertEqual(item["status"], "assigned_manual")


class DecisionTests(AppTestCase):
    def setUp(self):
        super().setUp()
        self.job = self.upload_and_get_job()
        self.set_lines(self.job, [0, 100, 200], [0, 100, 200, 300])
        self.current = self.state(self.job)

    def test_accepts_overlapping_cell(self):
        box = self.box_id(self.current, "6")
        response = self.client.post(
            f"/api/jobs/{self.job}/decisions",
            json={"box_id": box, "kind": "cell", "target_cell": "0|0|100|100"},
        )
        self.assertEqual(response.status_code, 200)
        item = next(entry for entry in response.get_json()["boxes"]
                    if entry["box_id"] == box)
        self.assertEqual(item["status"], "assigned_manual")
        self.assertEqual(item["column"], "name")

    def test_rejects_non_overlapping_cell(self):
        box = self.box_id(self.current, "6")
        response = self.client.post(
            f"/api/jobs/{self.job}/decisions",
            json={"box_id": box, "kind": "cell", "target_cell": "200|100|300|200"},
        )
        self.assertEqual(response.status_code, 400)
        self.assertFalse(response.get_json()["ok"])

    def test_rejects_decision_for_auto_box(self):
        box = self.box_id(self.current, "1")
        response = self.client.post(
            f"/api/jobs/{self.job}/decisions",
            json={"box_id": box, "kind": "cell", "target_cell": "0|0|100|100"},
        )
        self.assertEqual(response.status_code, 409)

    def test_exclude_box(self):
        box = self.box_id(self.current, "7")
        response = self.client.post(
            f"/api/jobs/{self.job}/decisions",
            json={"box_id": box, "kind": "exclude"},
        )
        self.assertEqual(response.status_code, 200)
        item = next(entry for entry in response.get_json()["boxes"]
                    if entry["box_id"] == box)
        self.assertEqual(item["status"], "excluded")

    def test_unknown_box_404(self):
        response = self.client.post(
            f"/api/jobs/{self.job}/decisions",
            json={"box_id": 99999, "kind": "exclude"},
        )
        self.assertEqual(response.status_code, 404)

    def test_clear_decision_returns_to_pending(self):
        box = self.box_id(self.current, "7")
        self.client.post(f"/api/jobs/{self.job}/decisions",
                         json={"box_id": box, "kind": "exclude"})
        response = self.client.delete(f"/api/jobs/{self.job}/decisions/{box}")
        self.assertEqual(response.status_code, 200)
        item = next(entry for entry in response.get_json()["boxes"]
                    if entry["box_id"] == box)
        self.assertEqual(item["status"], "pending")


class ExportTests(AppTestCase):
    def setUp(self):
        super().setUp()
        self.job = self.upload_and_get_job()
        self.set_lines(self.job, [0, 100, 200], [0, 100, 200, 300])

    def resolve_all(self):
        state = self.state(self.job)
        box6 = self.box_id(state, "6")
        box7 = self.box_id(state, "7")
        self.client.post(f"/api/jobs/{self.job}/decisions",
                         json={"box_id": box6, "kind": "cell",
                               "target_cell": "100|0|200|100"})
        self.client.post(f"/api/jobs/{self.job}/decisions",
                         json={"box_id": box7, "kind": "exclude"})

    def test_export_blocked_while_pending(self):
        response = self.client.get(f"/jobs/{self.job}/export.csv")
        self.assertEqual(response.status_code, 409)

    def test_export_blocked_without_valid_lines(self):
        self.set_lines(self.job, [0, 100], [0, 100, 200])
        response = self.client.get(f"/jobs/{self.job}/export.csv")
        self.assertEqual(response.status_code, 409)

    def test_export_succeeds_when_all_resolved(self):
        self.resolve_all()
        response = self.client.get(f"/jobs/{self.job}/export.csv")
        self.assertEqual(response.status_code, 200)
        self.assertIn("text/csv", response.headers["Content-Type"])
        text = response.data.decode("utf-8-sig")
        lines = [line for line in text.splitlines() if line.strip()]
        self.assertEqual(lines[0], "品名,规格,批发价")
        self.assertEqual(lines[1], "白砂糖,跨格一级,5.00")
        self.assertEqual(lines[2], "红糖,,1.20")

    def test_export_filename_is_utf8_encoded(self):
        self.resolve_all()
        response = self.client.get(f"/jobs/{self.job}/export.csv")
        disposition = response.headers["Content-Disposition"]
        self.assertIn("filename*=UTF-8''", disposition)
        self.assertIn("%E4%BB%B7%E7%9B%AE%E8%A1%A8", disposition)

    def test_export_uses_bom(self):
        self.resolve_all()
        response = self.client.get(f"/jobs/{self.job}/export.csv")
        self.assertTrue(response.data.startswith(b"\xef\xbb\xbf"))


class JobManagementTests(AppTestCase):
    def test_delete_job_removes_it(self):
        job_id = self.upload_and_get_job()
        response = self.client.post(f"/jobs/{job_id}/delete", follow_redirects=True)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.client.get(f"/api/jobs/{job_id}").status_code, 404)

    def test_missing_job_returns_404_page(self):
        response = self.client.get("/jobs/99999")
        self.assertEqual(response.status_code, 404)

    def test_suggest_lines_returns_three_columns(self):
        job_id = self.upload_and_get_job()
        response = self.client.post(f"/api/jobs/{job_id}/suggest-lines")
        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertEqual(len(data["vertical"]), 4)
        self.assertGreaterEqual(len(data["horizontal"]), 2)


BINDING_SAMPLE = """框号,文字,x1,y1,x2,y2
1,白砂糖,10,10,90,40
2,一级,110,10,190,40
3,5.00,210,10,290,40
4,跨格,90,10,110,40
"""


class BindingTests(AppTestCase):
    def make_job(self, body=SAMPLE, filename="page.csv"):
        response = self.upload(body=body, filename=filename)
        self.assertEqual(response.status_code, 302)
        return int(response.headers["Location"].rstrip("/").split("/")[-1])

    def bind(self, job_id, reference_id):
        return self.client.post(f"/api/jobs/{job_id}/binding",
                                json={"reference_job_id": reference_id})

    def unbind(self, job_id):
        return self.client.delete(f"/api/jobs/{job_id}/binding")

    def make_reference(self, vertical=(0, 100, 200, 300)):
        reference = self.make_job(BINDING_SAMPLE, "ref.csv")
        self.set_lines(reference, [0, 100, 200], list(vertical))
        return reference

    def make_target(self, vertical=(0, 100, 200, 300)):
        target = self.make_job(BINDING_SAMPLE, "target.csv")
        self.set_lines(target, [0, 100, 200], list(vertical))
        return target

    def test_bind_applies_reference_ratios_between_target_borders(self):
        reference = self.make_reference((0, 200, 500, 1000))
        target = self.make_target((0, 100, 200, 300))
        response = self.bind(target, reference)
        self.assertEqual(response.status_code, 200)
        state = self.state(target)
        # ratios 0.2 / 0.3 / 0.5 over the target's own 0..300 borders
        self.assertEqual(state["vertical"], [0, 60, 150, 300])
        self.assertEqual(state["binding"]["follows"], reference)
        self.assertFalse(state["binding"]["paused"])

    def test_follower_keeps_own_horizontals_and_outer_borders(self):
        reference = self.make_reference()
        target = self.make_target()
        self.bind(target, reference)
        response = self.set_lines(target, [0, 150, 250], [5, 70, 150, 400])
        self.assertEqual(response.status_code, 200)
        state = self.state(target)
        self.assertEqual(state["horizontal"], [0, 150, 250])
        # Verticals are still the followed ones, not the submitted values.
        self.assertEqual(state["vertical"], [0, 100, 200, 300])

    def test_reference_vertical_change_follows_through(self):
        reference = self.make_reference()
        target = self.make_target()
        self.bind(target, reference)
        response = self.set_lines(reference, [0, 100, 200], [0, 50, 250, 300])
        self.assertEqual(response.status_code, 200)
        state = self.state(target)
        self.assertEqual(state["vertical"], [0, 50, 250, 300])
        self.assertEqual(response.get_json()["followers"][0]["job_id"], target)

    def test_pause_when_converted_lines_invalid_keeps_old_width(self):
        reference = self.make_reference()
        target = self.make_target()
        self.bind(target, reference)
        before = self.state(target)["vertical"]
        # A hairline first column collapses on the target page.
        self.set_lines(reference, [0, 100, 200], [0, 0.01, 500, 1000])
        state = self.state(target)
        self.assertEqual(state["vertical"], before)
        self.assertTrue(state["binding"]["paused"])
        self.assertTrue(state["binding"]["pause_reasons"])

    def test_horizontal_only_reference_edit_does_not_resume_paused_follower(self):
        reference = self.make_reference()
        target = self.make_target()
        self.bind(target, reference)
        self.set_lines(reference, [0, 100, 200], [0, 0.01, 500, 1000])
        self.assertTrue(self.state(target)["binding"]["paused"])
        response = self.set_lines(reference, [0, 120, 260], [0, 0.01, 500, 1000])
        self.assertEqual(response.status_code, 200)
        # The (unusable) verticals did not change, so the follower stays
        # paused with its old lines rather than retrying or resuming.
        self.assertTrue(self.state(target)["binding"]["paused"])
        self.assertEqual(self.state(target)["vertical"], [0, 100, 200, 300])

    def test_pause_when_manual_decision_would_be_lost(self):
        reference = self.make_reference()
        target = self.make_target()
        self.bind(target, reference)
        box = self.box_id(self.state(target), "4")
        # Box 4 straddles x=100 with verticals 0/100/200/300.
        self.client.post(f"/api/jobs/{target}/decisions",
                         json={"box_id": box, "kind": "cell",
                               "target_cell": "0|0|100|100"})
        self.assertEqual(self.state(target)["status_counts"].get("assigned_manual"), 1)

        self.set_lines(reference, [0, 100, 200], [0, 60, 240, 300])
        state = self.state(target)
        self.assertEqual(state["vertical"], [0, 100, 200, 300])
        self.assertTrue(state["binding"]["paused"])
        affected = state["binding"]["affected_boxes"]
        self.assertEqual([entry["box_id"] for entry in affected], [box])
        self.assertEqual(affected[0]["box_ref"], "4")
        self.assertTrue(any("人工" in reason
                            for reason in state["binding"]["pause_reasons"]))
        # The decision itself is preserved in the database and can be fixed.
        item = next(entry for entry in state["boxes"] if entry["box_id"] == box)
        self.assertEqual(item["status"], "assigned_manual")

    def test_other_followers_still_update_when_one_pauses(self):
        reference = self.make_reference()
        losing = self.make_target()
        other = self.make_target()
        self.bind(losing, reference)
        self.bind(other, reference)
        box = self.box_id(self.state(losing), "4")
        self.client.post(f"/api/jobs/{losing}/decisions",
                         json={"box_id": box, "kind": "cell",
                               "target_cell": "0|0|100|100"})
        # Move x=100 -> 60: box 4 still touches the 0..? cells? Make the change
        # large enough to change box 4's intersecting cells on the target.
        self.set_lines(reference, [0, 100, 200], [0, 60, 240, 300])
        self.assertTrue(self.state(losing)["binding"]["paused"])
        other_state = self.state(other)
        self.assertFalse(other_state["binding"]["paused"])
        self.assertEqual(other_state["vertical"], [0, 60, 240, 300])

    def test_confirm_resumes_following_with_new_widths(self):
        reference = self.make_reference()
        target = self.make_target()
        self.bind(target, reference)
        box = self.box_id(self.state(target), "4")
        self.client.post(f"/api/jobs/{target}/decisions",
                         json={"box_id": box, "kind": "cell",
                               "target_cell": "0|0|100|100"})
        self.set_lines(reference, [0, 100, 200], [0, 60, 240, 300])
        self.assertTrue(self.state(target)["binding"]["paused"])

        response = self.client.post(f"/api/jobs/{target}/binding/confirm")
        self.assertEqual(response.status_code, 200)
        state = self.state(target)
        self.assertEqual(state["vertical"], [0, 60, 240, 300])
        self.assertFalse(state["binding"]["paused"])
        # Box 4 now sits wholly in the 60..240 column; the normal rules place
        # it automatically where the human had put it (spec), so the manual
        # choice survives without a new prompt.
        item = next(entry for entry in state["boxes"] if entry["box_id"] == box)
        self.assertEqual(item["status"], "assigned_auto")
        self.assertEqual(item["column"], "spec")

    def test_confirm_reopens_decision_when_auto_placement_differs(self):
        reference = self.make_reference()
        target = self.make_target()
        self.bind(target, reference)
        box = self.box_id(self.state(target), "4")
        # Human chose the name cell even though most of the box is in spec.
        self.client.post(f"/api/jobs/{target}/decisions",
                         json={"box_id": box, "kind": "cell",
                               "target_cell": "0|0|100|100"})
        # The new ratio puts box 4 wholly inside the spec column, which
        # contradicts the human's name-cell choice: pause rather than apply.
        self.set_lines(reference, [0, 100, 200], [0, 60, 240, 300])
        state = self.state(target)
        self.assertTrue(state["binding"]["paused"])
        self.assertEqual(
            [entry["box_id"] for entry in state["binding"]["affected_boxes"]],
            [box])
        item = next(entry for entry in state["boxes"] if entry["box_id"] == box)
        self.assertEqual(item["status"], "assigned_manual")

        # Confirming accepts the widths; the ordinary rules then place box 4
        # by geometry (spec), not by the now-contradicted manual choice.
        self.client.post(f"/api/jobs/{target}/binding/confirm")
        state = self.state(target)
        item = next(entry for entry in state["boxes"] if entry["box_id"] == box)
        self.assertEqual(item["status"], "assigned_auto")
        self.assertEqual(item["column"], "spec")

    def test_paused_page_can_still_edit_horizontals(self):
        reference = self.make_reference()
        target = self.make_target()
        self.bind(target, reference)
        self.set_lines(reference, [0, 100, 200], [0, 0.01, 500, 1000])
        self.assertTrue(self.state(target)["binding"]["paused"])
        response = self.set_lines(target, [0, 120, 220], [0, 100, 200, 300])
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.state(target)["horizontal"], [0, 120, 220])

    def test_unbind_keeps_lines_at_that_moment(self):
        reference = self.make_reference()
        target = self.make_target()
        self.bind(target, reference)
        self.set_lines(reference, [0, 100, 200], [0, 50, 250, 300])
        current = self.state(target)["vertical"]
        response = self.unbind(target)
        self.assertEqual(response.status_code, 200)
        state = self.state(target)
        self.assertEqual(state["vertical"], current)
        self.assertIsNone(state["binding"]["follows"])
        # After unbinding verticals are editable again.
        response = self.set_lines(target, [0, 100, 200], [0, 10, 20, 30])
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.state(target)["vertical"], [0, 10, 20, 30])

    def test_reference_cannot_follow_another_page(self):
        reference = self.make_reference()
        other = self.make_reference()
        target = self.make_target()
        self.bind(target, reference)
        response = self.bind(reference, other)
        self.assertEqual(response.status_code, 409)
        self.assertFalse(response.get_json()["ok"])

    def test_follower_cannot_be_used_as_reference(self):
        reference = self.make_reference()
        middle = self.make_target()
        self.bind(middle, reference)
        another = self.make_target()
        response = self.bind(another, middle)
        self.assertEqual(response.status_code, 409)

    def test_self_binding_rejected(self):
        reference = self.make_reference()
        response = self.bind(reference, reference)
        self.assertEqual(response.status_code, 400)

    def test_reference_page_cannot_be_deleted_while_bound(self):
        reference = self.make_reference()
        target = self.make_target()
        self.bind(target, reference)
        response = self.client.post(f"/jobs/{reference}/delete",
                                    follow_redirects=True)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.client.get(f"/api/jobs/{reference}").status_code, 200)
        self.unbind(target)
        self.client.post(f"/jobs/{reference}/delete", follow_redirects=True)
        self.assertEqual(self.client.get(f"/api/jobs/{reference}").status_code, 404)

    def test_six_follower_limit(self):
        reference = self.make_reference()
        targets = [self.make_target() for _ in range(7)]
        for target in targets[:6]:
            self.assertEqual(self.bind(target, reference).status_code, 200)
        response = self.bind(targets[6], reference)
        self.assertEqual(response.status_code, 409)
        self.assertIn("6", response.get_json()["error"])

    def test_bind_to_invalid_reference_rejected(self):
        reference = self.make_job(BINDING_SAMPLE, "ref.csv")
        # Corrupt the stored layout: one vertical line is not a usable grid.
        connection = db.connect(self.db_path)
        try:
            db.set_lines(connection, reference, [0, 100, 200], [0, 100, 200])
            connection.commit()
        finally:
            connection.close()
        target = self.make_target()
        response = self.bind(target, reference)
        self.assertEqual(response.status_code, 400)
        self.assertFalse(response.get_json()["ok"])

    def test_unknown_reference_404(self):
        target = self.make_target()
        self.assertEqual(self.bind(target, 99999).status_code, 404)

    def test_state_includes_binding_summary_for_both_sides(self):
        reference = self.make_reference()
        target = self.make_target()
        self.bind(target, reference)
        reference_state = self.state(reference)
        self.assertTrue(reference_state["binding"]["is_reference"])
        self.assertEqual(
            [entry["job_id"] for entry in reference_state["binding"]["followers"]],
            [target])
        target_state = self.state(target)
        self.assertEqual(target_state["binding"]["reference"]["id"], reference)

    def test_first_bind_pauses_when_manual_decision_would_be_lost(self):
        # The target's grid is set so box 4 straddles x=0 and x=... i.e. its
        # decisions are confirmed against the original lines.
        reference = self.make_reference((0, 200, 500, 1000))
        target = self.make_target((0, 100, 200, 300))
        box = self.box_id(self.state(target), "4")
        self.client.post(f"/api/jobs/{target}/decisions",
                         json={"box_id": box, "kind": "cell",
                               "target_cell": "0|0|100|100"})
        # Binding must not rewrite the target's verticals at all when that
        # would invalidate the confirmed decision.
        response = self.bind(target, reference)
        self.assertEqual(response.status_code, 200)
        state = self.state(target)
        self.assertTrue(state["binding"]["paused"])
        self.assertEqual(state["vertical"], [0, 100, 200, 300])
        self.assertEqual(
            [entry["box_id"] for entry in state["binding"]["affected_boxes"]],
            [box])

    def test_confirm_keeps_paused_when_widths_still_invalid(self):
        reference = self.make_reference()
        target = self.make_target()
        self.bind(target, reference)
        self.set_lines(reference, [0, 100, 200], [0, 0.01, 500, 1000])
        self.assertTrue(self.state(target)["binding"]["paused"])
        response = self.client.post(f"/api/jobs/{target}/binding/confirm")
        self.assertEqual(response.status_code, 409)
        self.assertTrue(self.state(target)["binding"]["paused"])
        self.assertEqual(self.state(target)["vertical"], [0, 100, 200, 300])

    def test_confirm_requires_a_paused_binding(self):
        reference = self.make_reference()
        target = self.make_target()
        self.assertEqual(
            self.client.post(f"/api/jobs/{target}/binding/confirm").status_code, 404)
        self.bind(target, reference)
        self.assertEqual(
            self.client.post(f"/api/jobs/{target}/binding/confirm").status_code, 409)

    def test_unbind_unknown_binding_404(self):
        target = self.make_target()
        self.assertEqual(self.unbind(target).status_code, 404)

    def test_preview_and_export_use_effective_lines(self):
        reference = self.make_reference()
        target = self.make_target((0, 100, 200, 300))
        self.bind(target, reference)
        # Without following, box 4 straddles the x=100 separator and is
        # pending.  Widen the reference's first column so the followed target
        # separator at x=75 moves left of box 4 (90..110): it now sits wholly
        # in the spec column.
        self.set_lines(reference, [0, 100, 200], [0, 250, 500, 1000])
        state = self.state(target)
        self.assertEqual(state["vertical"], [0, 75, 150, 300])
        item = next(entry for entry in state["boxes"] if entry["box_ref"] == "4")
        self.assertEqual(item["status"], "assigned_auto")
        self.assertEqual(item["column"], "spec")
        # Preview rows are computed against the followed lines, and export
        # follows the same state once nothing is pending.
        self.assertEqual(state["cells"][0]["rect"][:3], [0, 0, 75])
        for pending in [entry for entry in state["boxes"]
                        if entry["status"] in ("pending", "stale")]:
            self.client.post(
                f"/api/jobs/{target}/decisions",
                json={"box_id": pending["box_id"],
                      "kind": "cell" if pending["candidates"] else "exclude",
                      "target_cell": (pending["candidates"][0]["key"]
                                      if pending["candidates"] else None)})
        self.assertEqual(self.state(target)["pending"], 0)
        response = self.client.get(f"/jobs/{target}/export.csv")
        self.assertEqual(response.status_code, 200)
        self.assertIn("跨格".encode("utf-8"), response.data)

    def test_line_write_rolls_back_all_pages_on_failure(self):
        reference = self.make_reference()
        target = self.make_target()
        self.bind(target, reference)
        before_ref = self.state(reference)["vertical"]
        before_target = self.state(target)["vertical"]

        original = db.set_lines
        calls = {"n": 0}

        def failing_set_lines(connection, job_id, horizontal, vertical):
            calls["n"] += 1
            if calls["n"] >= 2:          # reference written, follower fails
                raise RuntimeError("disk full")
            return original(connection, job_id, horizontal, vertical)

        db.set_lines = failing_set_lines
        try:
            with self.assertRaises(RuntimeError):
                self.set_lines(reference, [0, 100, 200], [0, 40, 260, 300])
        finally:
            db.set_lines = original

        self.assertEqual(self.state(reference)["vertical"], before_ref)
        self.assertEqual(self.state(target)["vertical"], before_target)
        self.assertFalse(self.state(target)["binding"]["paused"])

    def test_bind_rolls_back_binding_when_line_write_fails(self):
        reference = self.make_reference()
        target = self.make_target()
        before = self.state(target)["vertical"]

        original = db.create_binding

        def failing_create(*args, **kwargs):
            raise RuntimeError("disk full")

        db.create_binding = failing_create
        try:
            with self.assertRaises(RuntimeError):
                self.bind(target, reference)
        finally:
            db.create_binding = original

        state = self.state(target)
        self.assertIsNone(state["binding"]["follows"])
        self.assertEqual(state["vertical"], before)

    def test_confirm_rolls_back_lines_when_binding_update_fails(self):
        reference = self.make_reference()
        target = self.make_target()
        self.bind(target, reference)
        self.set_lines(reference, [0, 100, 200], [0, 0.01, 500, 1000])
        self.assertTrue(self.state(target)["binding"]["paused"])
        before = self.state(target)["vertical"]
        await_reference = [0, 200, 500, 1000]
        self.set_lines(reference, [0, 100, 200], await_reference)

        original = db.update_binding

        def failing_update(*args, **kwargs):
            raise RuntimeError("disk full")

        db.update_binding = failing_update
        try:
            with self.assertRaises(RuntimeError):
                self.client.post(f"/api/jobs/{target}/binding/confirm")
        finally:
            db.update_binding = original

        state = self.state(target)
        self.assertEqual(state["vertical"], before)
        self.assertTrue(state["binding"]["paused"])


if __name__ == "__main__":
    unittest.main()
