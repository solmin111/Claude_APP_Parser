"""QA 피드백(2.1·2.3·2.4) 회귀 방지 — Claude Desktop 파서만.

2.1 서로 다른 대화의 동일 (역할·본문) 메시지가 중복으로 지워지면 안 된다.
2.3 여러 IndexedDB 저장소에서 복구한 메시지는 각자 자기 파일에서 나온 것으로 기록돼야 한다.
2.4 잠긴 파티션이 있어도 읽을 수 있는 NTFS 볼륨이 있으면 분석을 멈추지 않는다.
"""

import csv
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from parser import events, model, output          # noqa: E402
from parser import vfs                             # noqa: E402
from parser.artifacts import idbstore              # noqa: E402
from parser.model import RecordBuilder             # noqa: E402

IMG = {"image_id": "t", "image_type": "raw", "file_name": "x.dd"}


def _msg(rb, cid, muid, role, text, utc):
    det = {"artifact": "idb_leveldb", "item_type": "message", "text": text,
           "role": role, "conversation_uuid": cid, "message_uuid": muid,
           "role_known": True}
    return model.Record(
        rb.next_id(), "E04-01", events.name("E04-01"),
        model.timestamp(utc=utc, null_reason=None if utc else "시각 없음"),
        model.user_action("사용자 프롬프트 입력/전송", model.CONF_CONFIRMED),
        model.evidence(IMG, None), details=det)


def _write_read(records):
    fd, path = tempfile.mkstemp(suffix=".csv"); os.close(fd)
    try:
        output.write_conversations_csv(path, records)
        with open(path, encoding="utf-8-sig", newline="") as fh:
            return list(csv.DictReader(fh))
    finally:
        os.remove(path)


# ── 2.1 ───────────────────────────────────────────────────────────────
class ConversationDedup(unittest.TestCase):
    def test_diff_conversation_same_text_close_time_both_kept(self):
        rb = RecordBuilder()
        recs = [_msg(rb, "convA", "mA", "user", "안녕하세요", "2026-10-01T10:00:00Z"),
                _msg(rb, "convB", "mB", "user", "안녕하세요", "2026-10-01T10:00:01Z")]
        rows = _write_read(recs)
        self.assertEqual(len(rows), 2)                      # 다른 대화 → 둘 다 보존

    def test_diff_conversation_same_text_no_time_both_kept(self):
        rb = RecordBuilder()
        recs = [_msg(rb, "convA", "mA", "user", "네", None),
                _msg(rb, "convB", "mB", "user", "네", None)]
        rows = _write_read(recs)
        self.assertEqual(len(rows), 2)                      # 시각 없어도 다른 대화면 보존

    def test_same_message_two_stores_folded(self):
        rb = RecordBuilder()
        recs = [_msg(rb, "convA", "m1", "user", "같은 메시지", "2026-10-01T10:00:00Z"),
                _msg(rb, "convA", "m1", "user", "같은 메시지", "2026-10-01T10:00:01Z")]
        rows = _write_read(recs)
        self.assertEqual(len(rows), 1)                      # 같은 대화·같은 메시지 → 1행

    def test_same_conversation_no_id_duplicate_folded(self):
        rb = RecordBuilder()
        # 식별자 없는 초안류: 같은 대화·역할·본문, 하나는 시각 없음 → 접힘
        a = _msg(rb, "convA", None, "user", "초안 문장", "2026-10-01T10:00:00Z")
        b = _msg(rb, "convA", None, "user", "초안 문장", None)
        rows = _write_read([a, b])
        self.assertEqual(len(rows), 1)


# ── 2.3 ───────────────────────────────────────────────────────────────
class SourceAttribution(unittest.TestCase):
    def _w(self, vf, out):
        class V:
            path = vf
        return (V(), out)

    def test_each_db_maps_to_its_own_file(self):
        j = os.path.join
        dirA = j("tmp", "slotA", "https_claude.ai_0.indexeddb.leveldb")
        dirB = j("tmp", "slotB", "https_claude.ai_0.indexeddb.leveldb")
        written = [self._w(r"C:\A\...leveldb\000003.log", j(dirA, "000003.log")),
                   self._w(r"C:\A\...leveldb\000005.ldb", j(dirA, "000005.ldb")),
                   self._w(r"C:\B\...leveldb\000003.log", j(dirB, "000003.log"))]
        fb = object()
        self.assertEqual(idbstore._source_for(written, dirA, fb).path,
                         r"C:\A\...leveldb\000003.log")
        self.assertEqual(idbstore._source_for(written, dirB, fb).path,
                         r"C:\B\...leveldb\000003.log")

    def test_prefers_log_over_ldb(self):
        j = os.path.join
        d = j("tmp", "slotA", "store.leveldb")
        written = [self._w("x.ldb", j(d, "000005.ldb")),
                   self._w("x.log", j(d, "000003.log"))]
        self.assertEqual(idbstore._source_for(written, d, None).path, "x.log")

    def test_fallback_when_no_match(self):
        fb = object()
        self.assertIs(idbstore._source_for([], os.path.join("tmp", "none"), fb), fb)


# ── 2.4 ───────────────────────────────────────────────────────────────
class LockedPartitionGate(unittest.TestCase):
    def test_readable_plus_locked_proceeds(self):          # QA 표: NTFS 2 + 잠김 1 → 진행
        self.assertIsNone(vfs._abort_reason(volumes=[("vd", "fs")],
                                            locked=[{"index": 2}]))

    def test_readable_only_proceeds(self):
        self.assertIsNone(vfs._abort_reason(volumes=[("vd", "fs")], locked=[]))

    def test_no_readable_but_locked_is_locked(self):
        self.assertEqual(vfs._abort_reason(volumes=[], locked=[{"index": 1}]), "locked")

    def test_no_readable_no_locked_is_unsupported(self):
        self.assertEqual(vfs._abort_reason(volumes=[], locked=[]), "unsupported")


if __name__ == "__main__":
    unittest.main()
