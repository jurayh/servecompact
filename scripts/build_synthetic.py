"""Build fixtures/synthetic_sessions.json (4 hand-designed sessions).

Each session plants the 5 typed pins in a mid-conversation user
message (not just the system message) so marker extraction is exercised
in a non-protected position. Pin-bearing messages are never altered by
the compactor; the verifier is the backstop, proven by the replay
control (see replay.py) and the bad-compactor unit test.
"""

import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "..", "fixtures", "synthetic_sessions.json")

PINS = (
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


def verbose_log(n=180, error="ERROR: test_auth.py failed: assertion error",
                sig="def authenticate():"):
    lines = ["header: build log", "version: 1.2.3", sig]
    for i in range(n):
        lines.append(f"step {i}: repetitive build output " + "y" * 50)
    lines.insert(100, error)
    lines.append("done.")
    return "\n".join(lines)


def assistant_with_call(cid, name, args):
    return {"role": "assistant", "content": "",
            "tool_calls": [{"id": cid, "type": "function",
                            "function": {"name": name,
                                         "arguments": json.dumps(args)}}]}


def tool(cid, content):
    return {"role": "tool", "tool_call_id": cid, "content": content}


S1 = [
    {"role": "system", "content": "You are a deploy assistant."},
    {"role": "user", "content": "Fix the login bug and prepare a deploy."},
    {"role": "user", "content": PINS},
    assistant_with_call("r1", "Read", {"file_path": "auth.py"}),
    tool("r1", "def authenticate():\n    user = lookup_user(username)\n"
           "    if not user:\n        raise AuthError('unknown user')\n"
           "    return check_password(user, password)  # v1"),
    {"role": "assistant", "content":
     "STATUS: investigating the login failure; checking the token "
     "refresh path and the session store next."},
    {"role": "assistant", "content":
     "STATUS: investigating the login failure; checking the token "
     "refresh path and the session store next."},
    assistant_with_call("b1", "Bash", {"command": "pytest tests/"}),
    tool("b1", verbose_log()),
    assistant_with_call("r2", "Read", {"file_path": "auth.py"}),
    tool("r2", "def authenticate():\n    return check_password()  # v2, fixed"),
    {"role": "assistant", "content": "Port: 8080\nHost: localhost"},
    {"role": "user", "content": "Correction: Port is now 9090"},
    {"role": "user", "content": "What is the deploy region and the port?"},
]

S2 = [
    {"role": "system", "content": "You are a log reader."},
    {"role": "user", "content": PINS},
    assistant_with_call("l1", "Bash", {"command": "cat build.log"}),
    tool("l1", verbose_log(220, error="Traceback: File auth.py, line 9")),
    assistant_with_call("l2", "Bash", {"command": "cat test.log"}),
    tool("l2", verbose_log(160, error="ERROR: test_payments.py failed")),
    assistant_with_call("l3", "Bash", {"command": "cat audit.log"}),
    tool("l3", verbose_log(140, error="FATAL: disk full on /data")),
    {"role": "user", "content": "Summarise the three failures."},
]

S3 = [
    {"role": "system", "content": "You are a status bot."},
    {"role": "user", "content": PINS},
    assistant_with_call("s1", "Read", {"file_path": "status.txt"}),
    tool("s1", "STATUS: ready\nQueue: 12 jobs"),
    assistant_with_call("s2", "Read", {"file_path": "status.txt"}),
    tool("s2", "STATUS: ready\nQueue: 9 jobs"),
    assistant_with_call("s3", "Read", {"file_path": "status.txt"}),
    tool("s3", "STATUS: ready\nQueue: 0 jobs"),
    {"role": "assistant", "content": "STATUS: ready"},
    {"role": "assistant", "content": "STATUS: ready"},
    {"role": "user", "content": "Is the queue drained?"},
]

S4 = [
    {"role": "system", "content": "You are concise."},
    {"role": "user", "content": PINS},
    {"role": "assistant", "content": "The login bug is in the token refresh."},
    {"role": "user", "content": "Confirm the fix in one line."},
]

SESSIONS = [
    {"id": "synthetic-1-deploy-debug", "notes": "pins + superseded Read + dedup + correction + verbose log",
     "messages": S1},
    {"id": "synthetic-2-verbose-only", "notes": "three verbose logs, error/signature lines must survive",
     "messages": S2},
    {"id": "synthetic-3-repeat-heavy", "notes": "same file re-read 3x + repeated status",
     "messages": S3},
    {"id": "synthetic-4-small-clean", "notes": "no bloat; compactor must not invent savings",
     "messages": S4},
]


def main():
    with open(OUT, "w", encoding="utf-8") as fh:
        json.dump(SESSIONS, fh, indent=1)
    print(f"wrote {OUT} ({len(SESSIONS)} sessions)")


if __name__ == "__main__":
    main()
