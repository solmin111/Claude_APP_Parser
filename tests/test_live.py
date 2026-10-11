import contextlib
import csv
import hashlib
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from parser import cli  # noqa: E402
from parser.vfs import LiveVfs  # noqa: E402

LOG = "Users/tester/AppData/Local/Claude/logs/main.log"
CFG = "Users/tester/AppData/Roaming/Claude/config.json"
LOG_TEXT = (b"2026-09-23 13:03:58 [info] Starting Claude\n"
            b"2026-09-23 13:04:10 [info] Login-state transition: loggedOut: true -> false "
            b"uuid: <none> -> 11111111-2222-4333-8444-555555555555\n"
            b"2026-09-23 13:20:00 [info] some unrelated line\n"
            b"2026-09-23 13:24:51 [info] beforeQuit\n")


def utc(s):
    return datetime.fromisoformat(s).replace(tzinfo=timezone.utc).timestamp()


def run_cli(*args):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = cli.main(list(args) + ["--no-banner"])
    return code, out.getvalue(), err.getvalue()


def snapshot(root):
    snap = {}
    for d, _dirs, files in os.walk(root):
        for f in files:
            p = os.path.join(d, f)
            st = os.stat(p)
            with open(p, "rb") as fh:
                snap[os.path.relpath(p, root)] = (hashlib.sha256(fh.read()).hexdigest(),
                                                  st.st_mtime_ns, st.st_size)
    return snap


class LiveTest(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="cl_live_root_")
        self.work = tempfile.mkdtemp(prefix="cl_live_out_")
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.addCleanup(shutil.rmtree, self.work, ignore_errors=True)

    def put(self, rel, data, mtime=None):
        p = os.path.join(self.root, *rel.split("/"))
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "wb") as fh:
            fh.write(data)
        if mtime:
            os.utime(p, (utc(mtime), utc(mtime)))
        return p

    def claude_tree(self):
        self.put(LOG, LOG_TEXT, "2026-09-23T04:24:51")
        self.put(CFG, b'{"locale": "ko-KR", "userThemeMode": "system"}\n', "2026-09-23T04:24:50")
        self.put("Users/tester/.claude/settings.json", b'{"theme":"dark"}')
        self.put("Users/tester/Desktop/main.log", LOG_TEXT)

    def live(self, *extra):
        out = os.path.join(self.work, "out")
        code, stdout, err = run_cli("--input-type", "live", "-i", self.root, "-o", out,
                                    "--format", "both", *extra)
        return code, stdout, err, out

    def test_same_rows_as_image_and_live_provenance(self):
        self.claude_tree()
        code, stdout, err, out = self.live()
        self.assertEqual(code, 0, err)
        drive = os.path.splitdrive(self.root)[0].upper()
        rel_root = os.path.splitdrive(self.root)[1]
        with open(os.path.join(out, "timeline.csv"), encoding="utf-8-sig", newline="") as fh:
            rows = list(csv.DictReader(fh))
        got = [(r["Timestamp"], r["User_behavior"], r["Verdict"]) for r in rows]
        self.assertEqual(got, [
            ("2026-09-23 13:03:58.000000 KST", "애플리케이션 실행", "의심"),
            ("2026-09-23 13:04:10.000000 KST", "로그인", "확정"),
            ("2026-09-23 13:24:50.000000 KST", "정상 종료", "확정"),
            ("2026-09-23 13:24:51.000000 KST", "정상 종료", "확정"),
        ])
        log_path = drive + rel_root + "\\" + LOG.replace("/", "\\")
        self.assertEqual(rows[1]["Artifact_path"].lower(), log_path.lower())
        self.assertIn("수집=live", rows[1]["Artifact_details "])
        with open(os.path.join(out, "result.json"), encoding="utf-8") as fh:
            result = json.load(fh)
        self.assertEqual(result["input"]["image_type"], "live")
        self.assertIn("라이브    :", stdout)
        loc = result["records"][0]["evidence"]["location"]
        self.assertEqual(loc["time_source"], LiveVfs.TIME_SOURCE)
        self.assertIsNone(loc["ntfs_standard_information"]["mft_modified"])
        self.assertEqual(result["scan"]["files_scanned"], 2)
        self.assertIsNone(result["usn_journal"])

    def test_originals_unchanged(self):
        self.claude_tree()
        before = snapshot(self.root)
        code, _o, err, _out = self.live()
        self.assertEqual(code, 0, err)
        self.assertEqual(snapshot(self.root), before)

    def test_nothing_found_is_reported(self):
        self.put("Users/tester/Documents/a.txt", b"x")
        code, _o, err, out = self.live()
        self.assertEqual(code, 2)
        payload = json.loads(err)
        self.assertEqual(payload["code"], "ARTIFACT_NOT_FOUND")
        self.assertIn("이 PC", payload["message"])
        self.assertFalse(os.path.exists(out))

    def test_refuses_output_inside_evidence(self):
        self.claude_tree()
        inside = os.path.join(self.root, "Users", "tester", "AppData", "Roaming", "Claude", "out")
        before = snapshot(self.root)
        code, _o, err = run_cli("--input-type", "live", "-i", self.root, "-o", inside)
        self.assertEqual(code, 2)
        self.assertIn("분석 대상", err)
        self.assertFalse(os.path.exists(inside))
        self.assertEqual(snapshot(self.root), before)

    def test_input_required_unless_live(self):
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as cm:
                cli.main(["-o", os.path.join(self.work, "out"), "--no-banner"])
        self.assertEqual(cm.exception.code, 2)

    def test_folder_is_not_live_by_default(self):
        self.claude_tree()
        out = os.path.join(self.work, "out")
        code, _o, err = run_cli("-i", self.root, "-o", out, "--format", "json")
        self.assertEqual(code, 0, err)
        with open(os.path.join(out, "result.json"), encoding="utf-8") as fh:
            self.assertEqual(json.load(fh)["input"]["image_type"], "dir")

    @unittest.skipUnless(os.name == "nt", "Windows 공유 모드 시험")
    def test_file_held_by_running_app_is_read(self):
        self.claude_tree()
        handle = _hold(os.path.join(self.root, *LOG.split("/")), share=0x7, access=0xC0010000)
        try:
            code, _o, err, out = self.live()
        finally:
            _close(handle)
        self.assertEqual(code, 0, err)
        with open(os.path.join(out, "result.json"), encoding="utf-8") as fh:
            result = json.load(fh)
        self.assertEqual(result["filesystem"]["read_failures"], [])
        self.assertEqual(result["summary"]["record_counts"]["total"], 4)

    @unittest.skipUnless(os.name == "nt", "Windows 공유 모드 시험")
    def test_exclusively_locked_file_is_reported(self):
        self.claude_tree()
        handle = _hold(os.path.join(self.root, *LOG.split("/")), share=0, access=0x80000000)
        try:
            code, stdout, err, out = self.live()
        finally:
            _close(handle)
        self.assertEqual(code, 1, err)
        with open(os.path.join(out, "result.json"), encoding="utf-8") as fh:
            result = json.load(fh)
        (fail,) = result["filesystem"]["read_failures"]
        self.assertTrue(fail["locked"])
        self.assertEqual(result["errors"][0]["code"], "FILE_READ_FAILED")
        self.assertTrue(any("앱을 종료한 뒤" in w for w in result["warnings"]))
        self.assertIn("정상 종료", {r["user_action"]["action"] for r in result["records"]})


def _hold(path, share, access):
    import ctypes
    from ctypes import wintypes
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD,
                                wintypes.HANDLE]
    k32.CreateFileW.restype = wintypes.HANDLE
    h = k32.CreateFileW(path, access, share, None, 3, 0x80, None)
    if h == wintypes.HANDLE(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    return h


def _close(h):
    import ctypes
    ctypes.windll.kernel32.CloseHandle(h)


if __name__ == "__main__":
    unittest.main()
