"""Send a batch of varied test questions to the /chat API.

This populates rag_eval_log.csv with enough rows to make quality scores
statistically meaningful. Covers both SFC and PCPD topics, English and
Chinese, specific rules and broad conceptual questions.

Usage:
    python scripts/seed_eval_log.py

    # Or point at a different host/port:
    API_BASE=http://127.0.0.1:8001 python scripts/seed_eval_log.py
"""

import json
import os
import sys
import time

import requests

API_BASE = os.environ.get("API_BASE", "http://127.0.0.1:8000")
CHAT_URL = f"{API_BASE}/chat/"

QUESTIONS = [
    # SFC — client assets
    {"question": "What are the SFC requirements for client asset segregation?"},
    {"question": "How should a licensed corporation handle client money received?"},
    {"question": "What are the obligations of a futures broker regarding client margin?"},

    # SFC — licensing and conduct
    {"question": "What are the licensing requirements for virtual asset service providers under SFC?"},
    {"question": "What does the SFC require for internal audit functions at licensed corporations?"},
    {"question": "What are the SFC guidelines on managing conflicts of interest?"},
    {"question": "What anti-money laundering obligations apply to SFC-licensed firms?"},

    # PCPD — data protection
    {"question": "What does PCPD require for data retention and disposal?"},
    {"question": "What are the notification requirements when a data breach occurs under PCPD?"},
    {"question": "What rights do individuals have regarding their personal data under PCPD?"},
    {"question": "What obligations does a data user have when transferring data outside Hong Kong?"},

    # Cross-regulator / broader
    {"question": "What cybersecurity controls are required for financial institutions in Hong Kong?"},
    {"question": "What are the record-keeping requirements for licensed corporations?"},

    # Chinese-language queries (tests cross-lingual retrieval)
    {"question": "證監會對客戶資產保管有什麼要求？", "source_filter": "SFC"},
    {"question": "個人資料私隱專員公署對資料保留有什麼規定？", "source_filter": "PCPD"},

    # Source-filtered
    {"question": "What are the requirements for cold storage of virtual assets?", "source_filter": "SFC"},
    {"question": "How should organisations handle sensitive personal data?", "source_filter": "PCPD"},

    # Edge cases
    {"question": "What happens if a licensed corporation becomes insolvent?"},
    {"question": "Are there any exemptions from SFC licensing requirements?"},
]


def send(q: dict) -> bool:
    payload = {"question": q["question"]}
    if "source_filter" in q:
        payload["source_filter"] = q["source_filter"]
    try:
        r = requests.post(CHAT_URL, json=payload, timeout=120)
        if r.status_code == 200:
            ans = r.json().get("answer", "")[:80].replace("\n", " ")
            print(f"  ✓  {q['question'][:60]!r}")
            print(f"     → {ans}…")
            return True
        else:
            print(f"  ✗  HTTP {r.status_code}: {q['question'][:60]!r}")
            print(f"     {r.text[:120]}")
            return False
    except Exception as exc:
        print(f"  ✗  ERROR: {exc}  ({q['question'][:60]!r})")
        return False


def main():
    print(f"Sending {len(QUESTIONS)} questions to {CHAT_URL}\n")
    ok = fail = 0
    for i, q in enumerate(QUESTIONS, 1):
        print(f"[{i:02d}/{len(QUESTIONS)}]")
        if send(q):
            ok += 1
        else:
            fail += 1
        # small pause — avoids hammering the LLM API rate limits
        if i < len(QUESTIONS):
            time.sleep(1)

    print(f"\nDone — {ok} succeeded, {fail} failed.")
    print("Run `python scripts/eval_quality.py` to score all logged rows.")


if __name__ == "__main__":
    main()
