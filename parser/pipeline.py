import os
import time

from . import __version__, events, locate, timeline, usn as usn_mod
from .artifacts import idb as idb_art
from .artifacts import idbstore as idb_struct
from .artifacts import applog as applog_art
from .artifacts import misc as misc_art
from .errors import ArtifactNotFoundError, ClError
from .image import open_image
from . import model
from .model import RecordBuilder
from .vfs import DirectoryVfs, LiveVfs, TskImageVfs

PARSERS = {
    "idb_leveldb": idb_art.parse,
    "idb_blob": idb_art.parse,
    "local_storage": idb_art.parse,
    "session_storage": idb_art.parse,
    "cache_block": misc_art.parse_cache,
    "config": misc_art.parse_config,
    "prefetch": misc_art.parse_prefetch,
    "applog": applog_art.parse,
    "applog_pkg": applog_art.parse,
    "applog_shared": applog_art.parse,
    "setuplog": applog_art.parse,
}

DEFAULT_KINDS = tuple(PARSERS)


class Result(dict):
    pass


CORROBORATE_WINDOW = 120


PREFETCH_RUN_WINDOW = 60.0


def _merge_prefetch_runs(records):
    from datetime import datetime

    def secs(r):
        u = r["timestamp"].get("utc")
        return datetime.fromisoformat(u.replace("Z", "+00:00")).timestamp() if u else None

    groups = {}
    for r in records:
        det = r.get("details") or {}
        t = secs(r)
        if det.get("artifact") != "prefetch" or t is None or det.get("superseded_by"):
            continue
        groups.setdefault((r["event"]["event_id"], det.get("item_type")), []).append((t, r))

    merged = 0
    for items in groups.values():
        items.sort(key=lambda x: x[0])
        cluster = []
        for pair in items + [(None, None)]:
            if cluster and (pair[0] is None or pair[0] - cluster[-1][0] > PREFETCH_RUN_WINDOW):
                if len(cluster) >= 2:
                    merged += _fold_prefetch_cluster(cluster)
                cluster = []
            if pair[1] is not None:
                cluster.append(pair)
    return merged


def _fold_prefetch_cluster(cluster):
    rep = cluster[0][1]
    others = [r for _, r in cluster[1:]]
    n = len(cluster)
    rep_det = rep["details"]
    rep_det["summary"] = "prefetch_run=1/%d" % n
    rep_det["run_label"] = "prefetch_run=1/%d" % n
    from .output import to_kst
    times = "; ".join("같은 실행의 다른 프로세스 Prefetch %s"
                      % (to_kst(r["timestamp"].get("utc"))[11:19]) for r in others)
    rep_det["merge_note"] = times
    rep_det["merged_prefetch"] = [r["details"].get("file_name") for r in others]
    rep["parse"]["notes"].append("prefetch_run_merged=%d" % n)
    for r in others:
        r["details"]["superseded_by"] = rep["record_id"]
        r["parse"]["notes"].append("merged_into_prefetch_run=%s" % rep["record_id"])
    return len(others)


def _usn_secs(r):
    from datetime import datetime
    u = r["timestamp"].get("utc")
    return datetime.fromisoformat(u.replace("Z", "+00:00")).timestamp() if u else None


def _fold_usn_prefetch(records):
    by_id = {r["record_id"]: r for r in records}

    def visible(r):
        seen = set()
        while (r.get("details") or {}).get("superseded_by") and r["record_id"] not in seen:
            seen.add(r["record_id"])
            nxt = by_id.get(r["details"]["superseded_by"])
            if nxt is None:
                break
            r = nxt
        return r

    pf_by_lineage, reps = {}, []
    for r in records:
        det = r.get("details") or {}
        if det.get("artifact") != "prefetch":
            continue
        if det.get("lineage"):
            pf_by_lineage.setdefault(det["lineage"], r)
        if not det.get("superseded_by"):
            t = _usn_secs(r)
            if t is not None:
                reps.append((t, r["event"]["event_id"], r))

    from .output import to_kst
    folded = 0
    for r in records:
        det = r.get("details") or {}
        if (det.get("artifact") != "usn_journal" or det.get("match") != "prefetch"
                or det.get("key_reason") != "FILE_CREATE" or det.get("superseded_by")):
            continue
        t = _usn_secs(r)
        target = None
        lin = det.get("lineage")
        if lin and lin in pf_by_lineage:
            target = visible(pf_by_lineage[lin])
        elif t is not None:
            for tt, eid, rep in reps:
                if eid == r["event"]["event_id"] and abs(tt - t) <= PREFETCH_RUN_WINDOW:
                    target = rep
                    break
        if target is None or target["record_id"] == r["record_id"]:
            continue
        det["superseded_by"] = target["record_id"]
        r["parse"]["notes"].append("usn_prefetch_folded_into=%s" % target["record_id"])
        tdet = target["details"]
        add = "USN 생성 기록 %s" % (to_kst(r["timestamp"].get("utc"))[11:19])
        tdet["merge_note"] = (tdet["merge_note"] + "; " + add) if tdet.get("merge_note") else add
        target["parse"]["notes"].append("usn_prefetch_evidence=%s" % det.get("file_name"))
        folded += 1
    return folded


def _fold_package_installs(records):
    installs = []
    for r in records:
        det = r.get("details") or {}
        if r["event"]["event_id"] != "E02-01":
            continue
        t = _usn_secs(r)
        if t is None:
            continue
        if det.get("artifact") == "prefetch":
            installs.append((t, "setup_pf", r))
        elif det.get("artifact") == "usn_journal" and det.get("match") == "package":
            installs.append((t, "pkg", r))
    installs.sort(key=lambda x: x[0])
    downgraded, seen_install = 0, False
    for _t, kind, r in installs:
        if kind == "pkg" and seen_install:
            r["event"]["event_id"] = "NA-00"
            r["event"]["event_name"] = events.name("NA-00")
            r["details"]["candidate_event"] = "E02-01"
            r["user_action"] = model.user_action(
                observed=r["user_action"].get("observed_artifact") or r["details"].get("file_name"),
                rationale="WindowsApps 에 같은 패키지 폴더가 앞선 설치 신호 뒤 다시 생성됨, "
                          "자동 업데이트·MSIX 스테이징으로 보여 설치로 세지 않는다")
            r["parse"]["notes"].append("package_create_update_candidate")
            downgraded += 1
        else:
            seen_install = True
    return downgraded


def _corroborate(records):
    from datetime import datetime

    def secs(r):
        u = r["timestamp"].get("utc")
        if not u:
            return None
        return datetime.fromisoformat(u.replace("Z", "+00:00")).timestamp()

    buckets = {}
    for r in records:
        t = secs(r)
        if t is None or r["event"]["event_id"] == "NA-00":
            continue
        if (r.get("details") or {}).get("superseded_by"):
            continue
        buckets.setdefault(r["event"]["event_id"], []).append((t, r))

    upgraded = 0
    for eid, items in buckets.items():
        items.sort(key=lambda x: x[0])
        for i, (t, r) in enumerate(items):
            if r["user_action"]["confidence"] == model.CONF_CONFIRMED:
                continue
            mine = (r.get("details") or {}).get("artifact")
            lineage = (r.get("details") or {}).get("lineage")
            for t2, r2 in items:
                if abs(t2 - t) > CORROBORATE_WINDOW:
                    continue
                if (r2.get("details") or {}).get("artifact") in (None, mine):
                    continue
                if lineage and (r2.get("details") or {}).get("lineage") == lineage:
                    continue
                r2_art = (r2.get("details") or {}).get("artifact")
                if r2_art == "usn_journal":
                    continue
                if {mine, r2_art} == {"prefetch", "usn_journal"}:
                    continue
                r["user_action"]["confidence"] = model.CONF_CONFIRMED
                r["user_action"]["rationale"] = (
                    (r["user_action"].get("rationale") or "")
                    + " · 교차검증: %s 와 %s 가 같은 시각의 같은 행위를 가리킴"
                      % (mine, (r2.get("details") or {}).get("artifact")))
                r["parse"]["notes"].append("cross_channel_corroborated")
                upgraded += 1
                break
    return upgraded


NEAR_DUP_RATIO = 0.80


def _drop_duplicates(records, already, corpus):
    if not already:
        return records
    from difflib import SequenceMatcher

    out = []
    for r in records:
        t = (r.get("details") or {}).get("text")
        if t:
            flat = " ".join(t.split())
            if flat in already or any(a.startswith(flat) for a in already):
                continue
            if len(flat) >= 10 and flat in corpus:
                continue
            if _near_dup(flat, already, SequenceMatcher):
                continue
        out.append(r)
    return out


def _typing_prefix(shorter, longer):
    if len(shorter) < 2:
        return False
    return longer.startswith(shorter) or longer.startswith(shorter[:-1])


def _merge_snapshots(records):
    best, drop = {}, []
    for r in records:
        det = r.get("details") or {}
        if det.get("item_type") != "share_snapshot":
            continue
        key = det.get("snapshot_uuid") or det.get("title")
        prev = best.get(key)
        if prev is None:
            best[key] = r
            continue
        keep, lose = ((prev, r) if (prev["details"].get("conversation_uuid")
                                    or not r["details"].get("conversation_uuid"))
                      else (r, prev))
        best[key] = keep
        drop.append(lose)
    if drop:
        ids = {id(r) for r in drop}
        records[:] = [r for r in records if id(r) not in ids]
    return len(drop)


def _drop_undefined(records):
    keep, n = [], 0
    for r in records:
        if (r.get("details") or {}).get("item_type") in events.DROPPED_OBSERVATIONS:
            n += 1
            continue
        keep.append(r)
    records[:] = keep
    return n


def _collapse_drafts(records):
    drafts = [r for r in records
              if (r.get("details") or {}).get("item_type") == "draft"
              and not (r.get("details") or {}).get("superseded_by")]
    drafts.sort(key=lambda r: -len(r["details"]["text"]))
    kept, folded = [], 0
    for r in drafts:
        text = " ".join(r["details"]["text"].split())
        for k in kept:
            if _typing_prefix(text, " ".join(k["details"]["text"].split())):
                k["details"]["snapshots"] = (k["details"].get("snapshots", 1)
                                             + r["details"].get("snapshots", 1))
                r["details"]["superseded_by"] = "draft_snapshot_folded"
                r["parse"]["notes"].append("draft_snapshot_folded")
                folded += 1
                break
        else:
            kept.append(r)
    for k in kept:
        n = k["details"].get("snapshots", 1)
        if n > 1:
            k["user_action"]["rationale"] = (
                "입력 초안, 사용자가 입력창에 친 문장이다. 전송 여부는 "
                "확인되지 않았다 (타이핑 스냅샷 %d건을 완성 문장으로 접음)" % n)
    return folded


def _mark_superseded(records):
    n = 0
    for r in records:
        det = r.get("details") or {}
        if det.get("item_type") != "message":
            continue
        det["superseded_by"] = "structured_indexeddb"
        r["parse"]["notes"].append("superseded_by_structured_indexeddb")
        n += 1
    return n


def _near_dup(flat, already, matcher):
    if len(flat) < 10:
        return False
    for a in already:
        if abs(len(a) - len(flat)) > max(4, len(flat) * 0.2):
            continue
        if matcher(None, a, flat).quick_ratio() < NEAR_DUP_RATIO:
            continue
        if matcher(None, a, flat).ratio() >= NEAR_DUP_RATIO:
            return True
    return False


def run(input_path, input_type="auto", kinds=DEFAULT_KINDS, max_excerpt=500,
        max_file_bytes=64 * 1024 * 1024, usn=True, usn_max_bytes=None,
        verbose=False, key_material=None):
    started = time.time()
    warnings, errors = [], []

    def say(msg):
        if verbose:
            print("  %s" % msg, flush=True)

    if input_type == "live":
        vfs = LiveVfs(input_path)
        image_desc = vfs.describe_input()
        say("라이브 분석, %s (관리자 권한 %s)" % (vfs.root, image_desc["elevated"]))
        img = None
    elif input_type == "dir" or (input_type == "auto" and os.path.isdir(input_path)):
        vfs = DirectoryVfs(input_path)
        image_desc = {"image_id": os.path.abspath(input_path), "image_type": "dir",
                      "file_name": os.path.basename(os.path.abspath(input_path)),
                      "note": "추출 파일 트리 입력, 디스크 이미지 근거 없음"}
        img = None
    else:
        say("증거 여는 중, 분할 세그먼트 검증 포함...")
        img = open_image(input_path)
        image_desc = img.describe()
        say("이미지 %s · 세그먼트 %d개 · %s"
            % (image_desc["image_type"], len(image_desc["segments"]),
               image_desc["file_name"]))
        warnings.extend(image_desc.pop("warnings"))
        try:
            vfs = TskImageVfs(img, key_material=key_material)
        except ClError:
            img.close()
            raise
        for vd, _fs in vfs.volumes:
            if vd.get("encryption"):
                say("BitLocker 볼륨 해제, 파티션 %d (%s)" % (vd["index"], vd["unlock_method"]))
        for lk in vfs.locked:
            warnings.append("BitLocker 볼륨을 풀지 못해 건너뛰었습니다. 파티션 %d: %s"
                            % (lk["index"], lk["reason"]))
    fs_desc = vfs.describe()
    say("NTFS 볼륨 %d개, 대상 경로 훑는 중..."
        % len(getattr(vfs, "volumes", []) or [1]))

    targets, inventory, scanned, other = [], [], 0, {}
    for vfile in vfs.iter_files():
        scanned += 1
        if verbose and scanned % 2000 == 0:
            say("  훑은 파일 %d건 · 대상 %d건" % (scanned, len(inventory)))
        kind, meta = locate.classify(vfile.path)
        if kind is None:
            continue
        prod = locate.other_product(vfile.path)
        if prod:
            other[prod] = other.get(prod, 0) + 1
            continue
        inventory.append({"kind": kind, **vfile.location()})
        if kind in kinds:
            targets.append((kind, vfile, meta))

    if not inventory:
        if img is not None:
            img.close()
        raise ArtifactNotFoundError(
            "%s에서 Claude Desktop 아티팩트 경로를 하나도 찾지 못했습니다"
            % ("이 PC" if input_type == "live" else "이미지"),
            scanned_files=scanned,
            searched_patterns=[p[0] for p in locate._PATTERNS],
            **({"denied": getattr(vfs, "denied", [])} if input_type == "live" else {}))
    if other:
        warnings.append("이름만 겹치는 다른 제품의 경로는 제외했습니다: "
                        + ", ".join("%s %d건" % (k, v) for k, v in other.items()))
    if not targets:
        warnings.append("Claude 관련 경로 %d건을 찾았으나 파싱 대상 종류(%s)에 "
                        "해당하는 파일이 없습니다" % (len(inventory), ", ".join(kinds)))

    say("대상 %d건 확인, 파싱 시작" % len(targets))

    rb = RecordBuilder()
    records, parsed_files = [], []
    by_kind = {}

    idb_files = [vf for kind, vf, _ in targets
                 if kind in ("idb_leveldb", "idb_blob")]
    struct_recs, idb_summary = idb_struct.parse_group(
        idb_files, image_desc, rb, max_excerpt=max_excerpt, say=say)
    records.extend(struct_recs)
    if struct_recs:
        by_kind["idb_structured"] = len(struct_recs)
    if not idb_summary["available"]:
        warnings.append("IndexedDB 정식 파서(ccl_chromium_reader)를 불러오지 못해 "
                        "바이트 스캔만 수행했습니다: %s" % idb_summary["import_error"])
    elif not struct_recs and idb_files:
        warnings.append("IndexedDB 정식 파싱에서 대화를 얻지 못했습니다. "
                        "바이트 스캔 결과만 실립니다")
    already = idb_struct.recovered_texts(struct_recs)
    corpus = chr(10).join(already)
    superseded = 0

    for kind, vfile, _meta in sorted(targets, key=lambda t: t[1].path):
        if vfile.size > max_file_bytes:
            errors.append({"code": "FILE_TOO_LARGE", "path": vfile.path,
                           "size": vfile.size, "limit": max_file_bytes})
            continue
        try:
            recs = PARSERS[kind](vfile, image_desc, rb, max_excerpt=max_excerpt)
            if kind in ("idb_leveldb", "idb_blob", "local_storage", "session_storage"):
                recs = _drop_duplicates(recs, already, corpus)
            if kind in ("idb_leveldb", "idb_blob") and struct_recs:
                superseded += _mark_superseded(recs)
            records.extend(recs)
            parsed_files.append({"kind": kind, "path": vfile.path, "records": len(recs),
                                 "allocated": vfile.allocated,
                                 "mft_record": vfile.mft_record})
            if recs:
                by_kind[kind] = by_kind.get(kind, 0) + len(recs)
        except ClError as exc:
            errors.append({"path": vfile.path, **exc.to_dict()})
        except OSError as exc:
            errors.append({"path": vfile.path, "code": "FILE_READ_FAILED",
                           "message": "%s: %s" % (type(exc).__name__, exc)})
        except Exception as exc:  # noqa: BLE001
            errors.append({"path": vfile.path, "code": "UNEXPECTED",
                           "message": "%s: %s" % (type(exc).__name__, exc)})

    if getattr(vfs, "denied", None):
        warnings.append("권한이 없어 열지 못한 폴더 %d건, 관리자 권한으로 실행하면 더 볼 수 "
                        "있습니다 (filesystem.denied)" % len(vfs.denied))
    if getattr(vfs, "read_failures", None):
        locked = sum(1 for f in vfs.read_failures if f.get("locked"))
        warnings.append("열거는 됐지만 읽지 못한 파일 %d건%s (filesystem.read_failures)"
                        % (len(vfs.read_failures),
                           ", 그중 %d건은 실행 중인 Claude 앱이 잠근 파일이라 앱을 종료한 뒤 "
                           "다시 실행하면 읽을 수 있습니다" % locked if locked else
                           ", 권한이 없습니다"))

    for k, n in sorted(by_kind.items(), key=lambda x: -x[1]):
        say("[%s] 레코드 %d건" % (k, n))
    if superseded:
        say("이미 복원한 대화의 중복 조각 %d건 정리 "
            "(근거는 result.json 에 보존)" % superseded)
    idb_summary["byte_scan_superseded"] = superseded

    usn_summary = None
    if usn and img is not None:
        say("$UsnJrnl:$J 읽는 중...")
    if usn and img is not None and getattr(vfs, "volumes", None):
        usn_summary = []
        for vd, fs in vfs.volumes:
            loc = {"path": "/$Extend/$UsnJrnl:$J", "partition_index": vd["index"],
                   "partition_offset_bytes": vd["offset_bytes"]}
            try:
                info, recs_usn = usn_mod.read_journal(
                    fs, max_bytes=usn_max_bytes,
                    claude_dirs=getattr(vfs, "scanned_dirs", {}).get(vd["index"]))
            except Exception as exc:  # noqa: BLE001
                usn_summary.append({**loc, "status": "not_found",
                                    "message": "%s: %s" % (type(exc).__name__, exc)})
                continue
            records.extend(usn_mod.to_records(info, recs_usn, image_desc, rb, loc,
                                              max_excerpt=max_excerpt))
            usn_summary.append({**loc, "status": "ok", **info})

    merged = _merge_snapshots(records)
    if merged:
        say("같은 공유 스냅샷 %d건을 하나로 합침" % merged)


    _drop_undefined(records)

    folded = _collapse_drafts(records)
    if folded:
        say("입력 초안 타이핑 스냅샷 %d건을 완성 문장으로 접음" % folded)

    runs_merged = _merge_prefetch_runs(records)
    if runs_merged:
        say("같은 실행의 프리패치 %d건을 한 행으로 합침" % runs_merged)

    usn_folded = _fold_usn_prefetch(records)
    if usn_folded:
        say("같은 실행의 USN 프리패치 기록 %d건을 실행 행 근거로 합침" % usn_folded)

    pkg_folded = _fold_package_installs(records)
    if pkg_folded:
        say("반복된 패키지 폴더 생성 %d건을 업데이트 후보로 내림" % pkg_folded)

    say("교차검증 중...")
    corroborated = _corroborate(records)
    say("교차검증으로 확신도 상승 %d건" % corroborated)
    tl = timeline.build(records)
    say("레코드 %d건 · 소요 %.1f초" % (len(records), time.time() - started))
    status_counts = {}
    for r in records:
        s = r["parse"]["status"]
        status_counts[s] = status_counts.get(s, 0) + 1

    result = Result({
        "tool": {"name": "parser", "version": __version__,
                 "target": "Claude Desktop (MSIX Claude_pzs8sxrjxfjjc · "
                           "%APPDATA%\\Claude · %LOCALAPPDATA%\\Claude[-Data])"},
        "input": image_desc,
        "filesystem": fs_desc,
        "scan": {"files_scanned": scanned, "claude_paths_found": len(inventory),
                 "parse_targets": len(targets), "parsed_files": parsed_files,
                 "other_products_excluded": other},
        "indexeddb": idb_summary,
        "records": records,
        "timeline": tl["timeline"],
        "undated_records": tl["undated_records"],
        "summary": {"record_counts": tl["counts"],
                    "parse_status_counts": status_counts,
                    "cross_channel_corroborated": corroborated,
                    "elapsed_seconds": round(time.time() - started, 3)},
        "not_applicable": events.NOT_APPLICABLE,
        "artifact_inventory": inventory,
        "usn_journal": usn_summary,
        "warnings": warnings,
        "errors": errors,
    })
    if img is not None:
        img.close()
    return result
