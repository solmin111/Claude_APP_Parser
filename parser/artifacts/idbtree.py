from __future__ import annotations

import hashlib
import os
import re

_IMPORT_ERROR = None
try:
    from ccl_chromium_reader import ccl_chromium_indexeddb as _idb
except Exception as exc:                       # pragma: no cover
    _idb = None
    _IMPORT_ERROR = "%s: %s" % (type(exc).__name__, exc)

MAX_DEPTH = 14
MAX_LIST = 800

ROLE_MAP = {"human": "user", "user": "user", "assistant": "assistant",
            "system": "system", "model": "assistant"}

ORG_MARKERS = ("capabilities", "billing_type", "organization_type",
               "rate_limit_tier", "raven_settings", "api_disabled_reason")
PROJECT_MARKERS = ("creator", "archiver", "is_private", "prompt_template")

SYS_INJECT = re.compile(r"^\s*<(system-reminder|env|system)>", re.I)
UPLOAD_BLOCK = re.compile(r"^\s*<uploaded_files>", re.I)


def import_error():
    return _IMPORT_ERROR


_IDB_SPLIT = ".indexeddb."


def _slot(path):
    norm = path.replace(chr(92), "/")
    i = norm.lower().find(_IDB_SPLIT)
    holder = norm[:norm.rfind("/", 0, i)]
    tag = re.sub(r"[^0-9A-Za-z]+", "_", holder).strip("_").lower()
    return "%s_%s" % (tag[-48:], hashlib.sha1(holder.encode("utf-8")).hexdigest()[:8])


def materialize(vfiles, dst):
    dirs, written = {}, []
    for vfile in vfiles:
        norm = vfile.path.replace("\\", "/")
        low = norm.lower()
        i = low.find(_IDB_SPLIT)
        if i < 0:
            continue
        rel = norm[low.rfind("/", 0, i) + 1:]
        base = os.path.join(dst, _slot(vfile.path))
        if not vfile.allocated:
            base = os.path.join(base, "deleted_%s" % (vfile.mft_record or 0))
        out = os.path.join(base, *rel.split("/"))
        try:
            data = vfile.read()
        except Exception:
            continue
        os.makedirs(os.path.dirname(out), exist_ok=True)
        with open(out, "wb") as fh:
            fh.write(data)
        written.append((vfile, out))
        top = rel.split("/", 1)[0]
        dirs.setdefault((base, top), os.path.join(base, top))
    return dirs, written


def db_pairs(dirs):
    pairs = []
    for (base, top), path in sorted(dirs.items()):
        if not top.lower().endswith(".leveldb"):
            continue
        blob = os.path.join(base, top[:-len(".leveldb")] + ".blob")
        pairs.append((path, blob if os.path.isdir(blob) else None))
    return pairs


def open_db(leveldb_dir, blob_dir=None):
    if _idb is None:
        return None
    try:
        return _idb.WrappedIndexDB(str(leveldb_dir),
                                   str(blob_dir) if blob_dir else None)
    except Exception:
        return None


def iter_records(db):
    meta = getattr(db, "global_metadata", None) or getattr(
        getattr(db, "_raw_db", None), "global_metadata", None)
    try:
        ids = list(meta.db_ids)
    except Exception:
        return
    for dbid in ids:
        try:
            wrapped = db[dbid.dbid_no]
            names = list(wrapped.object_store_names)
        except Exception:
            continue
        for store_name in names:
            try:
                store = wrapped[store_name]
                it = store.iterate_records()
            except Exception:
                continue
            while True:
                try:
                    rec = next(it)
                except StopIteration:
                    break
                except Exception:
                    break
                yield (dbid.name, store_name,
                       rec.key.value if rec.key else None, rec.value,
                       bool(rec.is_live))


def content_text(content):
    kinds = []
    if isinstance(content, str):
        return content, kinds
    if not isinstance(content, list):
        return "", kinds
    parts = []
    for item in content:
        if isinstance(item, str):
            parts.append(item)
            continue
        if not isinstance(item, dict):
            continue
        t = str(item.get("type") or "")
        kinds.append(t or "?")
        if t in ("text", "") and isinstance(item.get("text"), str):
            parts.append(item["text"])
        elif t == "thinking":
            continue
        elif t in ("tool_use", "tool_result", "server_tool_use",
                   "web_search_tool_result", "knowledge"):
            continue
        elif isinstance(item.get("text"), str):
            parts.append(item["text"])
        elif item.get("content"):
            inner, ik = content_text(item["content"])
            kinds.extend(ik)
            if inner:
                parts.append(inner)
    return "\n".join(p for p in parts if p), kinds


def as_text(value):
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return str(value)


def _norm(uuid):
    return str(uuid).lower() if uuid else ""


def _msg(role, text, kinds, *, uuid, conversation, created_at=None,
         index=None, parent=None, model_id="", session="", source="",
         is_live=True, attachments=None, files=None):
    if not text or not text.strip():
        return None
    role = ROLE_MAP.get(str(role).lower(), "unknown")
    if role == "user" and ("tool_result" in kinds and "text" not in kinds):
        origin = "tool_result"
    elif SYS_INJECT.match(text):
        origin = "system_injection"
    elif UPLOAD_BLOCK.match(text):
        origin = "user_upload"
    elif role == "user":
        origin = "user_typed"
    else:
        origin = role
    return {"conversation_uuid": _norm(conversation), "message_uuid": _norm(uuid),
            "role": role, "origin": origin, "text": text.strip(),
            "created_at": created_at, "index": index,
            "parent_message_uuid": _norm(parent), "model": str(model_id or ""),
            "session_id": str(session or ""), "source": source,
            "is_live": is_live, "block_kinds": sorted(set(kinds)),
            "attachments": attachments or [], "files": files or []}


def from_chat_tree(tree, key, is_live):
    out = []
    conv = tree.get("uuid") or key
    model_id = tree.get("model") or ""
    for m in tree.get("chat_messages") or []:
        if not isinstance(m, dict):
            continue
        text, kinds = content_text(m.get("content"))
        if not text and isinstance(m.get("text"), str):
            text = m["text"]
        rec = _msg(m.get("sender") or m.get("role"), text, kinds,
                   uuid=m.get("uuid"), conversation=conv,
                   created_at=m.get("created_at"), index=m.get("index"),
                   parent=m.get("parent_message_uuid"), model_id=model_id,
                   source="conversation_store/chat", is_live=is_live,
                   attachments=m.get("attachments"), files=m.get("files"))
        if rec:
            out.append(rec)
    return out


def from_cowork_tree(tree, key, is_live):
    out = []
    for ev in tree.get("events") or []:
        if not isinstance(ev, dict):
            continue
        p = ev.get("payload")
        if not isinstance(p, dict):
            continue
        msg = p.get("message")
        if not isinstance(msg, dict):
            continue
        text, kinds = content_text(msg.get("content"))
        when = p.get("created_at") or ev.get("serverCreatedAt")
        rec = _msg(msg.get("role") or p.get("type"), text, kinds,
                   uuid=p.get("uuid") or ev.get("dedupKey"), conversation=key,
                   created_at=when, index=ev.get("seq"),
                   parent=p.get("parent_tool_use_id"),
                   model_id=msg.get("model") or "",
                   session=p.get("session_id") or "",
                   source="conversation_store/cowork", is_live=is_live)
        if rec:
            out.append(rec)
    return out


def walk_messages(node, out, depth=0, conversation=""):
    if depth > MAX_DEPTH:
        return
    if isinstance(node, dict):
        context = conversation
        if isinstance(node.get("uuid"), str) and (
                "chat_messages" in node or "current_leaf_message_uuid" in node):
            context = _norm(node["uuid"])
        has_id = any(k in node for k in ("uuid", "message_uuid"))
        has_sender = any(k in node for k in ("sender", "role", "author"))
        if has_id and has_sender:
            explicit = node.get("conversation_uuid") or node.get("chat_conversation_uuid")
            out.append((node, _norm(explicit) if explicit else context))
        for value in node.values():
            walk_messages(value, out, depth + 1, context)
    elif isinstance(node, list):
        for item in node[:MAX_LIST]:
            walk_messages(item, out, depth + 1, conversation)


def find_conversations(node, out, depth=0):
    if depth > MAX_DEPTH:
        return
    if isinstance(node, dict):
        if (isinstance(node.get("uuid"), str) and "name" in node
                and ("created_at" in node or "updated_at" in node)
                and "sender" not in node
                and not any(m in node for m in ORG_MARKERS)
                and not any(m in node for m in PROJECT_MARKERS)):
            out.append(node)
        for value in node.values():
            find_conversations(value, out, depth + 1)
    elif isinstance(node, list):
        for item in node[:MAX_LIST]:
            find_conversations(item, out, depth + 1)


def _fold(store, rec):
    key = rec["message_uuid"] or ("%s#%s" % (rec["conversation_uuid"], rec["text"][:40]))
    prev = store.get(key)
    if prev is None:
        store[key] = rec
        return
    if len(rec["text"]) > len(prev["text"]):
        prev["text"] = rec["text"]
        prev["origin"] = rec["origin"]
        prev["block_kinds"] = rec["block_kinds"]
    for k in ("conversation_uuid", "created_at", "index", "parent_message_uuid",
              "model", "session_id"):
        if not prev.get(k) and rec.get(k):
            prev[k] = rec[k]
    if rec["is_live"]:
        prev["is_live"] = True


def harvest(db):
    messages, conversations, opened = {}, {}, []
    stats = {"records": 0, "trees_chat": 0, "trees_cowork": 0,
             "generic": 0, "stores": {}}

    for dbname, store, key, value, is_live in iter_records(db):
        stats["records"] += 1
        sk = "%s/%s" % (dbname, store)
        stats["stores"][sk] = stats["stores"].get(sk, 0) + 1
        if not isinstance(value, dict):
            continue
        skey = str(key) if key is not None else ""

        tree = value.get("tree")
        if isinstance(tree, dict):
            if tree.get("chat_messages") is not None:
                stats["trees_chat"] += 1
                for rec in from_chat_tree(tree, skey, is_live):
                    _fold(messages, rec)
                if tree.get("uuid") and tree.get("name") is not None:
                    conversations.setdefault(_norm(tree["uuid"]), {}).update(
                        {"name": tree.get("name"), "created_at": tree.get("created_at"),
                         "updated_at": tree.get("updated_at"),
                         "model": tree.get("model") or value.get("product") or "",
                         "source": "conversation_store"})
            elif tree.get("events") is not None:
                stats["trees_cowork"] += 1
                for rec in from_cowork_tree(tree, skey, is_live):
                    _fold(messages, rec)
                conversations.setdefault(_norm(skey), {}).update(
                    {"name": conversations.get(_norm(skey), {}).get("name"),
                     "created_at": None, "source": "conversation_store",
                     "model": "", "product": value.get("product") or "cowork"})
            for k in ("accountUuid", "orgUuid", "messageCount", "sessionMarker"):
                if value.get(k) is not None:
                    conversations.setdefault(
                        _norm(value.get("conversationUuid") or skey), {})[k] = value[k]
            continue

        if store == "meta" and value.get("conversationUuid"):
            opened.append({"conversation_uuid": _norm(value["conversationUuid"]),
                           "last_opened_at": value.get("lastOpenedAt"),
                           "fetched_at": value.get("fetchedAt"),
                           "written_at": value.get("writtenAt"),
                           "message_count": value.get("messageCount"),
                           "product": value.get("product")})
            continue

        convs = []
        find_conversations(value, convs)
        for c in convs:
            meta = conversations.setdefault(_norm(c["uuid"]), {})
            for k, dst in (("name", "name"), ("created_at", "created_at"),
                           ("updated_at", "updated_at"), ("model", "model"),
                           ("is_temporary", "is_temporary"),
                           ("project_uuid", "project_uuid"),
                           ("is_starred", "is_starred"),
                           ("is_archived", "is_archived")):
                if c.get(k) is not None and meta.get(dst) in (None, ""):
                    meta[dst] = c[k]
            summary = as_text(c.get("summary"))
            if summary and len(summary) > len(meta.get("summary") or ""):
                meta["summary"] = summary
            meta.setdefault("source", "%s/%s" % (dbname, store))
        found = []
        walk_messages(value, found)
        for obj, conv in found:
            text, kinds = content_text(obj.get("content"))
            if not text and isinstance(obj.get("text"), str):
                text = obj["text"]
            rec = _msg(obj.get("sender") or obj.get("role") or obj.get("author"),
                       text, kinds, uuid=obj.get("uuid") or obj.get("message_uuid"),
                       conversation=conv, created_at=obj.get("created_at"),
                       index=obj.get("index"),
                       parent=obj.get("parent_message_uuid"),
                       model_id=obj.get("model") or "",
                       source="%s/%s" % (dbname, store), is_live=is_live,
                       attachments=obj.get("attachments"), files=obj.get("files"))
            if rec:
                stats["generic"] += 1
                _fold(messages, rec)

    out = list(messages.values())
    out.sort(key=lambda r: (r["conversation_uuid"], str(r["created_at"] or ""),
                            r["index"] if isinstance(r["index"], int) else 0))
    return {"messages": out, "conversations": conversations, "opened": opened,
            "stats": stats}
