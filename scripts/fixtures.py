"""Fixture tasks: deterministic information-preservation harness.

NOT an LLM eval. A small solver reads the prompt (original, compacted,
or sent-after-verification) and must return the expected answer. It
gates information loss, not model behaviour. The regression gate (K3)
applies to the SENT prompt: what the model would actually see.

Usage: python3 scripts/fixtures.py [--out reports/fixture_report.json]
"""

import argparse
import json
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from servecompact.pipeline import compact_and_verify  # noqa: E402
from servecompact.tokens import METHOD  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.join(HERE, "..")


def verbose_log():
    lines = ["header: build log", "def authenticate():"]
    for i in range(180):
        lines.append(f"step {i}: repetitive build output " + "y" * 50)
    lines.insert(100, "ERROR: test_auth.py failed: assertion error")
    lines.append("done.")
    return "\n".join(lines)


def _texts(messages):
    """Prompt text as a reader would see it: pin markers are wrappers,
    not content, so the solver strips them before extracting answers.
    (The compactor/verifier, by contrast, treat the markers as load
    bearing: a pin survives only if its inner text is verbatim.)"""
    out = []
    for m in messages:
        c = m.get("content")
        if isinstance(c, str):
            out.append(c)
        elif isinstance(c, list):
            out.extend(p.get("text", "") for p in c
                       if isinstance(p, dict) and p.get("type") == "text")
    text = "\n".join(out)
    text = re.sub(r"\[PIN:[a-z-]+\]", "", text)
    text = re.sub(r"\[/PIN\]", "", text)
    text = re.sub(r'<pin\s+type="[a-z-]+">', "", text, flags=re.I)
    text = re.sub(r"</pin>", "", text, flags=re.I)
    return text


def _line_prefix(text, prefix, last=False):
    hits = []
    for ln in text.split("\n"):
        stripped = ln.strip()
        if stripped.startswith(prefix):
            hits.append(stripped[len(prefix):].strip().rstrip("."))
    if not hits:
        return None
    return hits[-1] if last else hits[0]


def solve(task_id, messages):
    """Deterministic solver. Returns the answer string or None."""
    text = _texts(messages)
    if task_id == "t01-safety":
        return _line_prefix(text, "SAFE ACTION:")
    if task_id == "t02-hard-constraint":
        return _line_prefix(text, "REGION:")
    if task_id == "t03-decision":
        return _line_prefix(text, "DECISION:")
    if task_id == "t04-goal":
        return _line_prefix(text, "GOAL:")
    if task_id == "t05-fact":
        return _line_prefix(text, "OWNER:")
    if task_id == "t06-corrected-value":
        m = re.search(r"Correction:\s*\w+\s+is now\s+(\S+)", text)
        if m:
            return m.group(1)
        return _line_prefix(text, "Port:", last=True)
    if task_id == "t07-latest-tool-result":
        return _line_prefix(text, "VERSION:", last=True)
    if task_id == "t08-error-line":
        m = re.search(r"ERROR:\s*(\S+)\s+failed", text)
        return m.group(1) if m else None
    if task_id == "t09-signature-line":
        m = re.search(r"def\s+(\w+)\s*\(", text)
        return m.group(1) if m else None
    if task_id == "t10-duplicate-status":
        return _line_prefix(text, "STATUS:", last=True)
    if task_id == "t11-multi-pin":
        # All three pins share one line, so line-start matching cannot
        # see the second and third: extract each label wherever it sits.
        parts = []
        for label in ("SAFE ACTION", "DECISION", "OWNER"):
            m = re.search(label + r":\s*([^.]+)", text)
            parts.append(m.group(1).strip() if m else None)
        return " | ".join(parts) if all(parts) else None
    if task_id == "t12-superseded-marker":
        return _line_prefix(text, "PLAN:")
    raise ValueError(f"unknown task {task_id}")


def load_tasks():
    with open(os.path.join(ROOT, "fixtures", "tasks.json"),
              encoding="utf-8") as fh:
        tasks = json.load(fh)
    for task in tasks:
        for msg in task["messages"]:
            if msg.get("content") == "PLACEHOLDER_VERBOSE":
                msg["content"] = verbose_log()
    return tasks


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default=os.path.join(
        ROOT, "reports", "fixture_report.json"))
    args = parser.parse_args()
    rows = []
    for task in load_tasks():
        msgs = task["messages"]
        to_send, compacted, report = compact_and_verify(msgs)
        orig = solve(task["id"], msgs)
        comp = solve(task["id"], compacted)
        sent = solve(task["id"], to_send)
        rows.append({
            "id": task["id"], "title": task["title"],
            "expected": task["expected"],
            "without_proxy": orig,
            "pass_without_proxy": orig == task["expected"],
            "compacted_prompt": comp,
            "pass_compacted_prompt": comp == task["expected"],
            "sent_prompt": sent,
            "pass_sent_prompt": sent == task["expected"],
            "fallback": report["fallback"],
            "tokens_original": report["tokens_original"],
            "tokens_compacted": report["tokens_compacted"],
        })
    regressions = [r for r in rows
                   if r["pass_without_proxy"] and not r["pass_sent_prompt"]]
    summary = {
        "tasks": len(rows),
        "pass_without_proxy": sum(r["pass_without_proxy"] for r in rows),
        "pass_compacted_prompt": sum(r["pass_compacted_prompt"]
                                     for r in rows),
        "pass_sent_prompt": sum(r["pass_sent_prompt"] for r in rows),
        "regressions_sent_prompt": len(regressions),
        "regression_ids": [r["id"] for r in regressions],
        "kill_K3_hit": bool(regressions),
        "token_method": METHOD,
        "note": ("Deterministic harness, not an LLM eval. "
                 "Gate K3 applies to the sent prompt."),
    }
    report = {"summary": summary, "rows": rows}
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=1)
    print(f"{summary['pass_sent_prompt']}/{summary['tasks']} fixture tasks "
          f"pass with the proxy (sent prompt); "
          f"{summary['pass_without_proxy']}/{summary['tasks']} without it; "
          f"regressions: {summary['regressions_sent_prompt']} "
          f"[tokens: {METHOD}]")
    for r in rows:
        flag = "OK " if r["pass_sent_prompt"] else "FAIL"
        print(f"  {flag} {r['id']}: sent={r['sent_prompt']!r} "
              f"expected={r['expected']!r} fallback={r['fallback']}")
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
