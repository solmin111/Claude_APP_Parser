SCHEMA_VERSION = "1.0"

TS_SOURCE_CONTENT = "artifact_content"
TS_SOURCE_FILENAME = "artifact_filename"
TS_SOURCE_MFT = "ntfs_mft"
TS_SOURCE_NONE = "none"

CONF_CONFIRMED = "확인"
CONF_INFERRED = "추정"
CONF_UNKNOWN = "미확정"

ACTION_UNKNOWN = "미확정"


def timestamp(utc=None, original=None, fmt=None, meaning=None,
              source=TS_SOURCE_NONE, tz_note=None, null_reason=None):
    if utc is None and null_reason is None:
        null_reason = "근거에서 해석 가능한 시각을 찾지 못함"
    return {
        "utc": utc,
        "original_value": original,
        "original_format": fmt,
        "meaning": meaning,
        "source": source,
        "timezone_note": tz_note,
        "null_reason": null_reason,
    }


def user_action(action=ACTION_UNKNOWN, confidence=CONF_UNKNOWN, rationale=None,
                observed=None):
    return {
        "action": action,
        "confidence": confidence,
        "rationale": rationale,
        "observed_artifact": observed,
    }


def evidence(image, vfile, locator=None, excerpt=None, artifact_sha256=None,
             extra=None):
    ev = {
        "image": image,
        "location": vfile.location() if vfile is not None else None,
        "locator": locator,
        "artifact_sha256": artifact_sha256,
        "excerpt": excerpt,
    }
    if extra:
        ev.update(extra)
    return ev


class Record(dict):

    def __init__(self, record_id, event_id, event_name, ts, action, ev,
                 status="ok", errors=None, notes=None, details=None):
        super().__init__()
        self["schema_version"] = SCHEMA_VERSION
        self["record_id"] = record_id
        self["event"] = {"event_id": event_id, "event_name": event_name,
                         "catalog": "사용자 행위 분류표"}
        self["timestamp"] = ts
        self["user_action"] = action
        self["evidence"] = ev
        self["parse"] = {
            "status": status,
            "errors": errors or [],
            "notes": notes or [],
        }
        self["details"] = details or {}


class RecordBuilder:
    def __init__(self, prefix="R"):
        self._n = 0
        self._prefix = prefix

    def next_id(self):
        self._n += 1
        return "%s%06d" % (self._prefix, self._n)
