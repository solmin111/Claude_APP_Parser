import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from parser import model                       # noqa: E402
from parser.artifacts import applog, idb        # noqa: E402
from parser.model import RecordBuilder          # noqa: E402

IMG = {"image_id": "t"}
A = "fe704807-3aed-4482-9304-ac5c1fdf857e"
B = "11112222-3333-4444-5555-666677778888"


class FakeFile:
    def __init__(self, data, path, allocated=True):
        self.data = data if isinstance(data, bytes) else data.encode()
        self.path, self.allocated = path, allocated
        self.si_times = {"modified": "2026-09-23T15:06:12Z"}
        self.fn_times = {}
        self.mft_record = 7
        self.size = len(self.data)

    def read(self):
        return self.data

    def location(self):
        return {"path": self.path}


LOG_PATH = "/Users/u/AppData/Local/Claude/logs/main.log"


def _applog(lines):
    recs = applog.parse(FakeFile("\n".join(lines), LOG_PATH), IMG, RecordBuilder())
    return [(r["event"]["event_id"], r["user_action"]["confidence"]) for r in recs]


class AccountSwitchGuard(unittest.TestCase):
    def test_same_account_relogin_is_login_not_switch(self):
        ln = ("2026-09-23 15:06:12.100 Login-state transition: "
              "loggedOut: true -> false, uuid: %s -> %s" % (A, A))
        got = _applog([ln])
        self.assertIn(("E01-01", model.CONF_CONFIRMED), got)
        self.assertNotIn("E01-03", [e for e, _ in got])

    def test_genuine_switch_is_e0103(self):
        ln = ("2026-09-23 15:07:00.000 Login-state transition: "
              "account uuid: %s -> %s" % (A, B))
        got = _applog([ln])
        self.assertIn(("E01-03", model.CONF_CONFIRMED), got)

    def test_same_account_unicode_arrow(self):
        ln = ("2026-09-23 15:06:12.100 Login-state transition: "
              "loggedOut: true → false, uuid: %s → %s" % (A, A))
        self.assertNotIn("E01-03", [e for e, _ in _applog([ln])])


class UploadRefClassify(unittest.TestCase):
    def test_bare_filename(self):
        self.assertEqual(idb._upload_of("회의록_20261001.pdf"), ("E05-03", "문서 업로드"))

    def test_windows_path_doc(self):
        self.assertEqual(idb._upload_of(r"C:\Users\u\내부자료\회의록.docx"),
                         ("E05-03", "문서 업로드"))

    def test_unix_upload_path(self):
        self.assertEqual(idb._upload_of("/mnt/user-data/uploads/evidence.png"),
                         ("E05-01", "이미지 업로드"))

    def test_sentence_with_filename_is_not_upload(self):
        self.assertIsNone(idb._upload_of("회의록.pdf 요약해줘"))

    def test_plain_prompt_is_not_upload(self):
        self.assertIsNone(idb._upload_of("오늘 일정 정리해줘"))

    def test_unknown_extension_is_not_upload(self):
        self.assertIsNone(idb._upload_of("payload.bin"))


class DraftUploadPromotion(unittest.TestCase):

    def _run(self, drafts, monkey):
        monkey(lambda data: [(t, 100 + i, "store:chat-draft:", 1)
                             for i, t in enumerate(drafts)])
        data = b"store:chat-draft: claude.ai conversation"
        recs = idb.parse(FakeFile(data, "/x/IndexedDB/claude/000003.log"),
                         IMG, RecordBuilder())
        return recs

    def setUp(self):
        self._orig = idb._draft_texts

    def tearDown(self):
        idb._draft_texts = self._orig

    def _set(self, fn):
        idb._draft_texts = fn

    def test_upload_draft_becomes_e0503_suspect(self):
        recs = self._run(["/mnt/user-data/uploads/회의록_20261001.pdf"], self._set)
        up = [r for r in recs if r["details"].get("item_type") == "upload_ref"]
        self.assertEqual(len(up), 1)
        self.assertEqual(up[0]["event"]["event_id"], "E05-03")
        self.assertEqual(up[0]["user_action"]["confidence"], model.CONF_INFERRED)
        self.assertEqual(up[0]["details"]["file_name"], "회의록_20261001.pdf")

    def test_same_file_three_forms_fold_to_one(self):
        recs = self._run([
            "회의록_20261001.pdf",
            r"C:\Users\u\내부자료\회의록_20261001.pdf",
            "/mnt/user-data/uploads/회의록_20261001.pdf",
        ], self._set)
        up = [r for r in recs if r["details"].get("item_type") == "upload_ref"]
        self.assertEqual(len(up), 1)

    def test_normal_draft_stays_prompt(self):
        recs = self._run(["오늘 회의 요약 정리해줘"], self._set)
        drafts = [r for r in recs if r["details"].get("item_type") == "draft"]
        self.assertEqual(len(drafts), 1)
        self.assertEqual(drafts[0]["event"]["event_id"], "E04-01")
        self.assertFalse([r for r in recs if r["details"].get("item_type") == "upload_ref"])


if __name__ == "__main__":
    unittest.main()
