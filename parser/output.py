import csv
import json
import os
from datetime import datetime, timedelta

from . import events

MODEL = "claude-desktop"
PACKAGE_NAME = "Claude_pzs8sxrjxfjjc"
ROOT_PARENT = "00000000-0000-4000-8000-000000000000"

TIMELINE_COLUMNS = ["Model", "Timestamp", "User_behavior", "Behavior_details ", "Verdict",
                    "Artifact_path", "Artifact_details "]
CONV_COLUMNS = ["Model", "timestamp", "conversation_uuid", "conversation_title",
                "message_uuid", "parent_message_uuid", "message order", "role", "content",
                "Artifact_path"]

VERDICT = {"확인": "확정", "추정": "의심", "미확정": "판단 불가"}
CSV_VERDICTS = ("확정", "의심")

_ROLE = {"user": "user", "human": "user", "assistant": "assistant",
         "system": "system", "unknown": "unknown"}

_ORIGIN_ROLE = {"user_typed": "user", "user_upload": "user",
                "assistant": "assistant", "system": "system",
                "system_injection": "system", "tool_result": "tool",
                "unknown": "unknown"}
_TAG = {"cache_block": "Cache", "idb_leveldb": "IndexedDB", "idb_blob": "Blob",
        "local_storage": "Local Storage", "session_storage": "Session Storage",
        "config": "Config", "applog": "App Log", "applog_pkg": "App Log",
        "applog_shared": "App Log", "setuplog": "App Log", "prefetch": "Prefetch",
        "usn_journal": "USN"}
_CORE_KEYS = ("summary", "text", "prompt_text", "identifiers", "title", "model",
              "file_name", "key")

EMPTY = "-"


def write_json(path, payload):
    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
    return path


def to_kst(utc_iso):
    if not utc_iso:
        return ""
    dt = datetime.fromisoformat(utc_iso.replace("Z", "+00:00")) + timedelta(hours=9)
    return dt.strftime("%Y-%m-%d %H:%M:%S.%f") + " KST"


def _secs(utc_iso):
    return datetime.fromisoformat(utc_iso.replace("Z", "+00:00")).timestamp()


def _safe(v):
    s = "" if v is None else str(v)
    if not s.strip():
        return EMPTY
    return "'" + s if s[:1] in ("=", "+", "-", "@") else s


def _writer(fh):
    w = csv.writer(fh)
    return lambda row: w.writerow([_safe(c) for c in row])


def _one_line(v, limit=300):
    return " ".join(str(v).split())[:limit]


def _winpath(r):
    loc = (r.get("evidence") or {}).get("location") or {}
    p = loc.get("path") or ""
    return (loc.get("drive") or loc.get("live_root") or "C:") + p.replace("/", "\\") if p else ""


def _behavior_details(r, titles):
    det = r.get("details") or {}
    core = next((det[k] for k in _CORE_KEYS if det.get(k)), None)
    if core is None and det.get("conversation_uuid"):
        core = _title_of(titles, det.get("conversation_uuid"))
    if core is None:
        core = (r["user_action"].get("observed_artifact") or
                ((r.get("evidence") or {}).get("locator") or ""))
    return "%s_%s" % (r["user_action"].get("action"), _one_line(core))


def _artifact_details(r, titles):
    det, ev = r.get("details") or {}, r.get("evidence") or {}
    loc = ev.get("location") or {}
    tag = _TAG.get(det.get("artifact"), det.get("artifact") or "Artifact")
    kv = [("유형", det.get("item_type") or det.get("key") or det.get("reason")),
          ("대화 제목", det.get("title") or _title_of(titles, det.get("conversation_uuid"))),
          ("대화 UUID", det.get("conversation_uuid")),
          ("계정 UUID", det.get("account_uuid")),
          ("조직 UUID", det.get("org_uuid")),
          ("모델", det.get("model")),
          ("위치", det.get("run_label") or ev.get("locator")),
          ("병합", det.get("merge_note")),
          ("MFT", loc.get("mft_record")),
          ("volume", "ntfs@%s" % loc["partition_offset_bytes"]
           if loc.get("partition_offset_bytes") is not None else None),
          ("삭제 엔트리", "True" if loc.get("allocated") is False else None),
          ("수집", "live" if loc.get("time_source") else None),
          ("규칙", r["event"]["event_id"]),
          ("패키지", PACKAGE_NAME)]
    return "[%s] " % tag + ", ".join("%s=%s" % (k, _one_line(v, 120))
                                     for k, v in kv if v not in (None, ""))


def _titles(records):
    titles = {}
    for r in records:
        det = r.get("details") or {}
        if det.get("title") and det.get("conversation_uuid"):
            titles.setdefault(str(det["conversation_uuid"]).lower(),
                              det["title"])
    return titles


def _title_of(titles, cid):
    return titles.get(str(cid or "").lower(), "")


def write_timeline_csv(path, records):
    titles = _titles(records)
    rows = [r for r in records if r["event"]["event_id"] != "NA-00"
            and VERDICT.get(r["user_action"].get("confidence")) in CSV_VERDICTS
            and not (r.get("details") or {}).get("superseded_by")]
    rows.sort(key=lambda r: (r["timestamp"].get("utc") is None,
                             r["timestamp"].get("utc") or "", r["record_id"]))
    with open(path, "w", encoding="utf-8-sig", newline="") as fh:
        w = _writer(fh)
        w(TIMELINE_COLUMNS)
        for r in rows:
            w([MODEL, to_kst(r["timestamp"].get("utc")),
               events.name(r["event"]["event_id"]),
               _behavior_details(r, titles),
               VERDICT[r["user_action"].get("confidence")],
               _winpath(r), _artifact_details(r, titles)])
    return path


def write_conversations_csv(path, records):
    titles = _titles(records)
    msgs, seen, seen_ids = [], {}, set()
    for r in records:
        det = r.get("details") or {}
        role = _ROLE.get((det.get("role") or "").lower())
        if role is None or not det.get("text") or det.get("superseded_by"):
            continue
        if det.get("origin"):
            role = _ORIGIN_ROLE.get(det["origin"], role)
        if role == "unknown":
            continue
        cid = det.get("conversation_uuid") or ""
        muid = det.get("message_uuid")
        utc = r["timestamp"].get("utc")
        if muid:
            idkey = (cid, muid)
            if idkey in seen_ids:
                continue
            seen_ids.add(idkey)
        else:
            key = (cid, role, " ".join(det["text"].split()))
            times = seen.get(key)
            if times is not None:
                if any(u and utc and abs(_secs(u) - _secs(utc)) <= 2 for u in times):
                    continue
                if not utc:
                    continue
            seen.setdefault(key, []).append(utc)
        msgs.append((cid, utc or "", r["record_id"], role, det, r))
    msgs.sort(key=lambda m: (m[0], m[1], m[2]))
    with open(path, "w", encoding="utf-8-sig", newline="") as fh:
        w = _writer(fh)
        w(CONV_COLUMNS)
        prev_cid, prev_uuid, order = None, None, 0
        for cid, utc, rid, role, det, r in msgs:
            if cid != prev_cid:
                prev_cid, prev_uuid, order = cid, ROOT_PARENT, 0
            order += 1
            uuid = det.get("message_uuid") or "%s-%d" % (cid or "msg", order)
            parent = det.get("parent_message_uuid") or prev_uuid
            w([MODEL, to_kst(utc), cid, _title_of(titles, cid), uuid, parent,
               order, role, det["text"], _winpath(r)])
            prev_uuid = uuid
    return path
