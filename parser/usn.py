import re
import struct
from datetime import datetime, timedelta, timezone

from . import events, model

CLAUDE_PF = re.compile(r"^(?P<exe>CLAUDE[^\\/]*?\.EXE)-[0-9A-F]{8}\.pf$", re.I)
PACKAGE_DIR = re.compile(r"^Claude_[^_\\/]+_(x64|x86|arm64|neutral)__pzs8sxrjxfjjc$", re.I)
PACKAGE_DATA_DIR = re.compile(r"^Claude_pzs8sxrjxfjjc$", re.I)

SKIP_DIR_PREFIXES = ("/windows/prefetch", "/program files/windowsapps")
BLOB_DIR_MARK = ".indexeddb.blob"
FILE_ATTRIBUTE_DIRECTORY = 0x10

REASONS = [
    (0x00000001, "DATA_OVERWRITE"), (0x00000002, "DATA_EXTEND"), (0x00000004, "DATA_TRUNCATION"),
    (0x00000010, "NAMED_DATA_OVERWRITE"), (0x00000020, "NAMED_DATA_EXTEND"),
    (0x00000040, "NAMED_DATA_TRUNCATION"), (0x00000100, "FILE_CREATE"),
    (0x00000200, "FILE_DELETE"), (0x00000400, "EA_CHANGE"), (0x00000800, "SECURITY_CHANGE"),
    (0x00001000, "RENAME_OLD_NAME"), (0x00002000, "RENAME_NEW_NAME"),
    (0x00004000, "INDEXABLE_CHANGE"), (0x00008000, "BASIC_INFO_CHANGE"),
    (0x00010000, "HARD_LINK_CHANGE"), (0x00020000, "COMPRESSION_CHANGE"),
    (0x00040000, "ENCRYPTION_CHANGE"), (0x00080000, "OBJECT_ID_CHANGE"),
    (0x00100000, "REPARSE_POINT_CHANGE"), (0x00200000, "STREAM_CHANGE"),
    (0x00400000, "TRANSACTED_CHANGE"), (0x00800000, "INTEGRITY_CHANGE"),
    (0x80000000, "CLOSE"),
]
KEY_REASONS = ("FILE_CREATE", "FILE_DELETE")

_EPOCH = datetime(1601, 1, 1, tzinfo=timezone.utc)


def _filetime(v):
    if not v:
        return None
    try:
        return (_EPOCH + timedelta(microseconds=v // 10)).isoformat().replace("+00:00", "Z")
    except (OverflowError, ValueError):
        return None


def _reason_names(mask):
    return [n for bit, n in REASONS if mask & bit]


def iter_records(data, base_offset=0):
    off, n = 0, len(data)
    while off + 8 <= n:
        (length, major, minor) = struct.unpack_from("<IHH", data, off)
        if length == 0:
            off += 8
            continue
        if length < 56 or length > 1024 or off + length > n:
            off += 8
            continue
        try:
            if major == 2:
                (fref, pref, usn, ts, reason, src, sec, attrs, nlen, noff) = \
                    struct.unpack_from("<QQQqIIIIHH", data, off + 8)
            elif major == 3:
                (fref_lo, fref_hi, pref_lo, pref_hi, usn, ts, reason, src, sec, attrs, nlen, noff) = \
                    struct.unpack_from("<QQQQQqIIIIHH", data, off + 8)
                fref, pref = fref_lo, pref_lo
            else:
                off += 8
                continue
            if noff + nlen > length or nlen == 0:
                off += length
                continue
            name = data[off + noff: off + noff + nlen].decode("utf-16-le", "replace")
        except (struct.error, ValueError):
            off += 8
            continue
        yield base_offset + off, {
            "usn": usn, "file_mft_record": fref & 0xFFFFFFFFFFFF, "file_seq": fref >> 48,
            "parent_mft_record": pref & 0xFFFFFFFFFFFF, "parent_seq": pref >> 48,
            "timestamp_utc": _filetime(ts), "reasons": _reason_names(reason),
            "reason_mask": reason, "source_info": src, "security_id": sec,
            "file_attributes": attrs, "name": name, "version": "%d.%d" % (major, minor),
        }
        off += length


class Selector:

    def __init__(self, claude_dirs):
        self.dirs = {k: p for k, p in (claude_dirs or {}).items()
                     if not p.lower().startswith(SKIP_DIR_PREFIXES)}

    def _parent(self, rec):
        key = (rec["parent_mft_record"], rec["parent_seq"])
        if key in self.dirs:
            return self.dirs[key]
        if rec["parent_seq"] == 0:
            for (mft, _seq), path in self.dirs.items():
                if mft == rec["parent_mft_record"]:
                    return path
        return None

    def match(self, rec):
        name = rec["name"]
        parent = self._parent(rec)
        if parent is not None:
            rec["parent_path"] = parent
            rec["match"] = "claude_dir"
            if (rec["file_attributes"] & FILE_ATTRIBUTE_DIRECTORY
                    and "FILE_CREATE" in rec["reasons"]):
                self.dirs.setdefault((rec["file_mft_record"], rec["file_seq"]),
                                     parent + "/" + name)
            return True
        if CLAUDE_PF.match(name):
            rec["match"] = "prefetch"
            return True
        if PACKAGE_DIR.match(name) or PACKAGE_DATA_DIR.match(name):
            rec["match"] = "package"
            return True
        return False


def read_journal(fs, chunk=4 << 20, max_bytes=None, claude_dirs=None):
    import pytsk3
    f = fs.open("/$Extend/$UsnJrnl")
    attr = next((a for a in f
                 if a.info.name and a.info.name.decode("utf-8", "replace") == "$J"
                 and int(a.info.type) == int(pytsk3.TSK_FS_ATTR_TYPE_NTFS_DATA)), None)
    if attr is None:
        raise FileNotFoundError("$UsnJrnl:$J 스트림을 찾지 못함")
    selector = Selector(claude_dirs)
    size = attr.info.size
    limit = min(size, max_bytes) if max_bytes else size
    out, scanned, tail, total = [], 0, b"", 0
    first_ts = last_ts = None
    off = 0
    while off < limit:
        want = min(chunk, limit - off)
        try:
            buf = f.read_random(off, want, pytsk3.TSK_FS_ATTR_TYPE_NTFS_DATA, attr.info.id)
        except OSError:
            break
        if not buf:
            break
        if buf.count(0) == len(buf):
            off += want
            tail = b""
            continue
        data = tail + buf
        base = off - len(tail)
        last_end = 0
        for rec_off, rec in iter_records(data, base):
            last_end = max(last_end, rec_off - base)
            total += 1
            ts = rec["timestamp_utc"]
            if ts:
                first_ts = min(first_ts or ts, ts)
                last_ts = max(last_ts or ts, ts)
            if selector.match(rec):
                out.append(rec)
        scanned += len(buf)
        tail = data[last_end:] if len(data) - last_end < 1024 else b""
        off += want
    return {"journal_size_bytes": size, "scanned_bytes": scanned,
            "records_total": total, "records_matched": len(out),
            "claude_dirs_compared": len(selector.dirs),
            "match_basis": "Claude 탐색 폴더(부모 MFT#·순번, 저널 중 새 하위 폴더 포함) + "
                           "Claude 프리패치·패키지 폴더 이름",
            "journal_earliest_utc": first_ts, "journal_latest_utc": last_ts,
            "note": "저널은 순환하므로 earliest 이전 행위는 USN 에 남지 않는다"}, out


def to_records(usn_info, usn_recs, image_desc, rb, journal_location, max_excerpt=500):
    records = []
    grouped = {}
    key_events = {}
    for r in usn_recs:
        gkey = r.get("parent_path") or r["match"]
        g = grouped.setdefault(gkey, {"reasons": {}, "first": None, "last": None, "count": 0,
                                      "names": set(), "match": r["match"]})
        g["count"] += 1
        g["names"].add(r["name"])
        for name in r["reasons"]:
            g["reasons"][name] = g["reasons"].get(name, 0) + 1
        ts = r["timestamp_utc"]
        if ts:
            g["first"] = min(g["first"] or ts, ts)
            g["last"] = max(g["last"] or ts, ts)
        for reason in [x for x in r["reasons"] if x in KEY_REASONS]:
            if _classify(r, reason) is None:
                continue
            kkey = (r["file_mft_record"], r["file_seq"], r["name"], reason)
            prev = key_events.get(kkey)
            if prev is None:
                key_events[kkey] = {"record": r, "reason": reason, "count": 1,
                                    "last": r["timestamp_utc"]}
            else:
                prev["count"] += 1
                prev["last"] = r["timestamp_utc"] or prev["last"]

    for ke in key_events.values():
        records.append(_key_record(ke["record"], ke["reason"], ke["count"], ke["last"],
                                   image_desc, rb, journal_location))

    for where, g in sorted(grouped.items()):
        names = sorted(g["names"])
        records.append(model.Record(
            rb.next_id(), "NA-00", events.name("NA-00"),
            model.timestamp(utc=g["first"], original=g["first"], fmt="FILETIME(UTC)",
                            meaning="이 폴더 아래 USN 저널 최초 기록 시각",
                            source=model.TS_SOURCE_CONTENT),
            model.user_action(
                observed="USN 저널에 %s 아래 변경 %d건 · 이름 %d개 (%s)" % (
                    where if g["match"] == "claude_dir" else g["match"],
                    g["count"], len(names), ", ".join(sorted(g["reasons"]))),
                rationale="변경 사유 집계, 개별 행위로 특정하지 않음"),
            model.evidence(image_desc, None, locator="usn_summary %s" % where,
                           extra={"location": journal_location}),
            status="ok",
            notes=["USN 레코드에는 경로가 없음, 위치는 부모 참조로 복원"],
            details={"artifact": "usn_journal", "item_type": "usn_folder_summary",
                     "file_name": names[0], "names": names[:20], "name_count": len(names),
                     "parent_path": where if g["match"] == "claude_dir" else None,
                     "match": g["match"], "reason_counts": g["reasons"],
                     "record_count": g["count"], "first_utc": g["first"], "last_utc": g["last"]}))
    return records


def _is_dir(r):
    return bool(r["file_attributes"] & FILE_ATTRIBUTE_DIRECTORY)


def _classify(r, reason):
    name, match = r["name"], r["match"]
    if match == "prefetch" and reason == "FILE_CREATE":
        exe = CLAUDE_PF.match(name).group("exe").upper()
        lineage = "prefetch:%s" % name.upper()
        if "SETUP" in exe:
            return ("E02-01", "애플리케이션 설치", model.CONF_INFERRED,
                    "Claude 설치 프로그램(%s)의 프리패치 파일이 처음 만들어짐, 설치 프로그램 "
                    "실행 시각" % exe, None, lineage)
        return ("E08-01", "애플리케이션 실행", model.CONF_INFERRED,
                "Claude 실행 파일(%s)의 프리패치 파일이 처음 만들어짐, 그 시점의 실행 흔적"
                % exe, None, lineage)
    if match == "package" and _is_dir(r):
        if PACKAGE_DIR.match(name) and reason == "FILE_CREATE":
            return ("E02-01", "애플리케이션 설치", model.CONF_INFERRED,
                    "WindowsApps 에 Claude 패키지 폴더 생성, 설치 또는 버전 업데이트", None,
                    "package:%s" % name.lower())
        if PACKAGE_DIR.match(name) and reason == "FILE_DELETE":
            return ("NA-00", model.ACTION_UNKNOWN, model.CONF_UNKNOWN,
                    "WindowsApps 의 Claude 패키지 폴더 삭제, 앱 삭제와 버전 업데이트를 "
                    "이 기록만으로는 구분 불가", "E02-02", "package:%s" % name.lower())
        if PACKAGE_DATA_DIR.match(name) and reason == "FILE_DELETE":
            return ("E02-02", "애플리케이션 삭제", model.CONF_INFERRED,
                    "사용자 데이터 폴더 Packages\\Claude_pzs8sxrjxfjjc 삭제, 앱 제거 시 지워지는 폴더",
                    None, "package_data")
        if PACKAGE_DATA_DIR.match(name) and reason == "FILE_CREATE":
            return ("NA-00", model.ACTION_UNKNOWN, model.CONF_UNKNOWN,
                    "사용자 데이터 폴더 Packages\\Claude_pzs8sxrjxfjjc 생성, 설치 뒤 첫 실행 무렵",
                    "E02-01/E08-01", "package_data")
        return None
    if match == "claude_dir" and BLOB_DIR_MARK in (r.get("parent_path") or "").lower() \
            and not _is_dir(r):
        if reason == "FILE_CREATE":
            return ("NA-00", model.ACTION_UNKNOWN, model.CONF_UNKNOWN,
                    "IndexedDB 첨부 저장소에 본문 파일 생성, 첨부 업로드 흔적. 이미지·문서 "
                    "구분은 이 기록만으로 불가", "E05-01/E05-03", None)
        return ("NA-00", model.ACTION_UNKNOWN, model.CONF_UNKNOWN,
                "IndexedDB 첨부 저장소의 본문 파일 삭제, 첨부 삭제 또는 앱 정리",
                "E05-02/E05-04", None)
    return None


def _key_record(r, reason, count, last_utc, image_desc, rb, journal_location):
    event_id, action, conf, why, candidate, lineage = _classify(r, reason)
    path_hint = ((r.get("parent_path") or "") + "/" + r["name"]) if r.get("parent_path") else None
    return model.Record(
        rb.next_id(), event_id, events.name(event_id),
        model.timestamp(utc=r["timestamp_utc"], original=r["timestamp_utc"],
                        fmt="FILETIME(UTC)",
                        meaning="USN 저널에 기록된 변경 시각",
                        source=model.TS_SOURCE_CONTENT),
        model.user_action(action=action, confidence=conf, rationale=why,
                          observed="%s: %s (같은 사유 USN 레코드 %d건, 마지막 %s)"
                                   % (r["name"], reason, count, last_utc)),
        model.evidence(image_desc, None,
                       locator="usn=%d file_mft=%d" % (r["usn"], r["file_mft_record"]),
                       extra={"location": journal_location}),
        status="ok",
        notes=["USN 시각은 저널 기록 시각, 사용자 행위 시각과 동일하다고 단정하지 않음",
               "USN 은 파일이 닫힐 때까지 사유 비트를 누적, 같은 사유의 첫 기록만 레코드로 남김"],
        details={"artifact": "usn_journal", "file_name": r["name"], "key_reason": reason,
                 "candidate_event": candidate, "lineage": lineage, "path_hint": path_hint,
                 "same_reason_record_count": count, "last_same_reason_utc": last_utc,
                 **{k: v for k, v in r.items() if k not in ("parent_path",)}})
