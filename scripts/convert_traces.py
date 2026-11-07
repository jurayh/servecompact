"""Convert trace-commons Claude Code JSONL sessions to OpenAI messages.

Corpus: public trace-commons sessions (NOT Yuriy's own sessions; this
VM has none). Each converted session is prepended with one system
message carrying the 5 planted pinned canaries (one per type), so the
replay measures token reduction on real session shapes plus pin
survival on planted canaries.

Conversion rules (same pairing as the Sluice phase-0 measurement):
- assistant records: text blocks joined as content; tool_use blocks
  become tool_calls (id, name, arguments JSON).
- user records: tool_result blocks become tool messages (tool_call_id
  + text); text blocks become a user message.
- sidechain records, queue operations, attachments, snapshots, and
  titles are skipped: they are not conversation turns.
"""

import glob
import json
import os

PIN_SYSTEM = (
    "[PIN:safety] CANARY-SAFETY: never run DROP TABLE on the production "
    "database. [/PIN]\n"
    "[PIN:hard-constraint] CANARY-CONSTRAINT: deploys only to us-west1, "
    "never to eu-central. [/PIN]\n"
    "[PIN:decision] CANARY-DECISION: use Postgres for the session store, "
    "not SQLite. [/PIN]\n"
    "[PIN:goal] CANARY-GOAL: ship the login fix before Friday's freeze. "
    "[/PIN]\n"
    "[PIN:fact] CANARY-FACT: the service owner is team Atlas, on-call "
    "rotation R7. [/PIN]"
)


def _block_text(block):
    content = block.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for x in content:
            if isinstance(x, dict) and x.get("type") == "text":
                parts.append(x.get("text") or "")
            elif isinstance(x, dict) and "text" in x:
                parts.append(str(x["text"]))
            elif isinstance(x, str):
                parts.append(x)
        return "\n".join(parts)
    return ""


def convert_file(path):
    messages = []
    for line in open(path, encoding="utf-8", errors="replace"):
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if rec.get("isSidechain") is True:
            continue
        rtype = rec.get("type")
        if rtype == "assistant":
            content = (rec.get("message") or {}).get("content") or []
            texts, calls = [], []
            for block in content:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "text":
                    texts.append(block.get("text") or "")
                elif block.get("type") == "tool_use":
                    calls.append({
                        "id": block.get("id"),
                        "type": "function",
                        "function": {
                            "name": block.get("name"),
                            "arguments": json.dumps(
                                block.get("input") or {}),
                        },
                    })
            msg = {"role": "assistant", "content": "\n".join(texts)}
            if calls:
                msg["tool_calls"] = calls
            messages.append(msg)
        elif rtype == "user":
            content = (rec.get("message") or {}).get("content")
            if isinstance(content, str):
                if content.strip():
                    messages.append({"role": "user", "content": content})
                continue
            if isinstance(content, list):
                texts = []
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    if block.get("type") == "tool_result":
                        messages.append({
                            "role": "tool",
                            "tool_call_id": block.get("tool_use_id"),
                            "content": _block_text(block),
                        })
                    elif block.get("type") == "text":
                        texts.append(block.get("text") or "")
                if any(t.strip() for t in texts):
                    messages.append({"role": "user",
                                     "content": "\n".join(texts)})
    system = {"role": "system", "content": PIN_SYSTEM}
    return [system] + messages


def convert_dir(trace_dir):
    sessions = []
    for path in sorted(glob.glob(os.path.join(trace_dir, "*.jsonl"))):
        messages = convert_file(path)
        sessions.append({
            "id": os.path.basename(path).replace(".jsonl", ""),
            "source": path,
            "messages": messages,
        })
    return sessions
