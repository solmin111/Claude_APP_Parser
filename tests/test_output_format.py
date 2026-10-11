import csv
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from parser import model, output, pipeline  # noqa: E402

IMG = {"image_id": "t"}


def rec(eid, action, conf, det, utc="2026-10-02T03:00:00Z", rid=None):
    return model.Record(rid or ("r%d" % (id(det) % 100000)), eid, eid,
                        model.timestamp(utc=utc), model.user_action(action, conf),
                        {"location": {"path": "/x"}}, details=det)


def _rows(path):
    with open(path, encoding="utf-8-sig") as fh:
        return list(csv.DictReader(fh))


def write_timeline(records):
    d = tempfile.mkdtemp()
    output.write_timeline_csv(d + "/t.csv", records)
    return _rows(d + "/t.csv")


def write_conv(records):
    d = tempfile.mkdtemp()
    output.write_conversations_csv(d + "/c.csv", records)
    return _rows(d + "/c.csv")


class BehaviorDetailsLabel(unittest.TestCase):
    def test_applog_uses_short_label_not_raw_line(self):
        r = rec("E01-01", "로그인", model.CONF_CONFIRMED,
                {"artifact": "applog", "summary": "인증 상태 변경(로그인)",
                 "log_body": "2026-10-01 22:27:04 [info] [account] Login-state transition ...",
                 "account_uuid": "fe704807-3aed-4482-9304-ac5c1fdf857e"})
        (row,) = write_timeline([r])
        self.assertEqual(row["Behavior_details "], "로그인_인증 상태 변경(로그인)")
        self.assertNotIn("[info]", row["Behavior_details "])
        self.assertNotIn("2026-10-01 22:27:04", row["Behavior_details "])
        self.assertNotIn("Login-state transition", row["Behavior_details "])

    def test_raw_log_body_never_reaches_behavior_details(self):
        r = rec("E08-02", "정상 종료", model.CONF_INFERRED,
                {"artifact": "applog",
                 "log_body": "2026/10/02 00:42:44 Shutdown signal received..."})
        (row,) = write_timeline([r])
        self.assertNotIn("Shutdown signal received", row["Behavior_details "])


class ConversationRoleFilter(unittest.TestCase):
    def test_unknown_role_fragments_excluded_user_kept(self):
        recs = [
            rec("E04-01", "사용자 프롬프트 입력/전송", model.CONF_INFERRED,
                {"artifact": "idb_leveldb", "role": "user", "origin": "user_typed",
                 "text": "내 자산 점검 결과 정리해줘", "conversation_uuid": "c1"}),
            rec("NA-00", "x", model.CONF_UNKNOWN,
                {"artifact": "idb_leveldb", "role": "unknown",
                 "text": "Nmap scan report for host", "conversation_uuid": "-"}),
            rec("NA-00", "x", model.CONF_UNKNOWN,
                {"artifact": "idb_leveldb", "role": "unknown",
                 "text": "Host is up (0.00017s latency).", "conversation_uuid": "-"}),
        ]
        rows = write_conv(recs)
        self.assertEqual([r["role"] for r in rows], ["user"])
        self.assertNotIn("unknown", {r["role"] for r in rows})

    def test_assistant_and_tool_still_kept(self):
        recs = [
            rec("NA-00", "x", model.CONF_INFERRED,
                {"artifact": "idb_leveldb", "role": "assistant", "origin": "assistant",
                 "text": "정리해 드리겠습니다", "conversation_uuid": "c1",
                 "message_uuid": "m2", "parent_message_uuid": "m1"}),
        ]
        rows = write_conv(recs)
        self.assertEqual([r["role"] for r in rows], ["assistant"])


class PrefetchRunMerge(unittest.TestCase):
    def _pf(self, name, utc, rid):
        return rec("E08-01", "애플리케이션 실행", model.CONF_INFERRED,
                   {"artifact": "prefetch", "file_name": name, "item_type": "CLAUDE.EXE",
                    "lineage": "prefetch:%s" % name.upper()}, utc=utc, rid=rid)

    def test_same_run_folds_to_one_row(self):
        recs = [self._pf("CLAUDE.EXE-725B.pf", "2026-10-01T14:57:46Z", "a"),
                self._pf("CLAUDE.EXE-725C.pf", "2026-10-01T14:57:46Z", "b"),
                self._pf("CLAUDE.EXE-725D.pf", "2026-10-01T14:57:47Z", "c"),
                self._pf("CLAUDE.EXE-7266.pf", "2026-10-01T14:57:48Z", "d")]
        merged = pipeline._merge_prefetch_runs(recs)
        self.assertEqual(merged, 3)
        rep = [r for r in recs if not (r["details"].get("superseded_by"))]
        self.assertEqual(len(rep), 1)
        self.assertEqual(rep[0]["record_id"], "a")
        self.assertEqual(rep[0]["details"]["run_label"], "prefetch_run=1/4")
        rows = write_timeline(recs)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["Behavior_details "], "애플리케이션 실행_prefetch_run=1/4")

    def test_distant_runs_stay_separate(self):
        recs = [self._pf("CLAUDE.EXE-A.pf", "2026-10-01T14:57:46Z", "a"),
                self._pf("CLAUDE.EXE-B.pf", "2026-10-01T15:03:23Z", "b")]
        merged = pipeline._merge_prefetch_runs(recs)
        self.assertEqual(merged, 0)
        self.assertEqual(len(write_timeline(recs)), 2)

    def test_setup_prefetch_not_merged_with_run(self):
        recs = [rec("E02-01", "애플리케이션 설치", model.CONF_INFERRED,
                    {"artifact": "prefetch", "file_name": "CLAUDE SETUP.EXE-1.pf",
                     "item_type": "CLAUDE SETUP.EXE"}, utc="2026-10-01T14:57:46Z", rid="s"),
                self._pf("CLAUDE.EXE-A.pf", "2026-10-01T14:57:46Z", "a")]
        self.assertEqual(pipeline._merge_prefetch_runs(recs), 0)


if __name__ == "__main__":
    unittest.main()
