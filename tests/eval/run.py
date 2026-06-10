# tests/eval/run.py
"""
Golden Q&A 회귀 하네스(F-2).

각 케이스에 대해 '실제 답변 파이프라인'(prepare_ask → run_claude_stream, A 즉답 포함)을
운영 DB에 '쓰지 않고'(읽기만) 실행해 답변·출처·의도·토큰을 모으고 결정적으로 검사한다.

검사(기본, 결정적):
  - expect_intent: prepare_ask 가 정한 intent(inventory|content) 일치
  - must_include : 답변에 모두 부분 포함
  - must_not     : 답변에 하나도 없음
  - must_cite    : 실제 사용 출처(S-0 sources)에 부분 포함

옵션:
  --judge   : haiku LLM 심판으로 의미 일치까지 채점(기본 꺼짐)
  --case ID : 특정 케이스만 실행
  --limit N : 앞에서 N개만

사용:
  python -m tests.eval.run
  python -m tests.eval.run --judge
  python -m tests.eval.run --case sh02_content

운영 DB는 읽기만 한다(prepare_ask/run_claude_stream 은 저장하지 않음). 리포트는
tests/eval/report_<timestamp>.md 로 저장한다.
"""
import argparse
import json
import time
from pathlib import Path

from app.database import get_source_by_delimiter
from app.router import prepare_ask, verify_citations
from app.claude_runner import run_claude, run_claude_stream

GOLDEN = Path(__file__).parent / "golden.jsonl"
REPORT_DIR = Path(__file__).parent


def load_cases() -> list[dict]:
    cases = []
    with open(GOLDEN, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                cases.append(json.loads(line))
    return cases


def _source_strings(sources: list[dict]) -> list[str]:
    return [f"{s.get('path', '')} {s.get('title', '')}" for s in sources]


def _est_cost(u: dict) -> float:
    """가중 토큰 비용(doc): input + 0.1×cache_read + 1.25×cache_write."""
    return ((u.get("input_tokens") or 0)
            + 0.1 * (u.get("cache_read") or 0)
            + 1.25 * (u.get("cache_write") or 0))


def _judge(question: str, answer: str, case: dict) -> tuple[bool, str]:
    """haiku 심판: 답변이 기준(포함/금지 의도)을 의미적으로 만족하는지 채점."""
    crit = {
        "must_include": case.get("must_include", []),
        "must_not": case.get("must_not", []),
        "expect_intent": case.get("expect_intent", ""),
    }
    prompt = (
        "너는 QA 답변 채점기다. 아래 '질문'에 대한 '답변'이 '기준'을 만족하는지 판정해라.\n"
        "반드시 JSON 한 줄로만 답하라: {\"pass\": true/false, \"reason\": \"...\"}\n\n"
        f"기준: {json.dumps(crit, ensure_ascii=False)}\n\n"
        f"질문: {question}\n\n"
        f"답변:\n{answer[:4000]}\n"
    )
    try:
        raw = run_claude(prompt, model="haiku")
        import re
        m = re.search(r"\{.*\}", raw, re.DOTALL)
        data = json.loads(m.group(0)) if m else {}
        return bool(data.get("pass")), str(data.get("reason", ""))[:200]
    except Exception as e:
        return False, f"judge 오류: {e}"


def run_case(case: dict, use_judge: bool) -> dict:
    cid = case["id"]
    delim = case.get("source")
    src = get_source_by_delimiter(delim) if delim else None
    if src is None or not src.get("enabled"):
        return {"id": cid, "status": "SKIP", "reason": f"출처 {delim} 미등록",
                "checks": [], "usage": {}, "intent": None}

    session = "eval_" + cid  # 임시 세션(저장 안 함)
    try:
        prep = prepare_ask(case["question"], session, src["id"], dev_view=False)
    except Exception as e:
        return {"id": cid, "status": "ERROR", "reason": str(e),
                "checks": [], "usage": {}, "intent": None}
    if prep.get("needs_source"):
        return {"id": cid, "status": "ERROR", "reason": "needs_source(라우팅 실패)",
                "checks": [], "usage": {}, "intent": None}

    intent = prep.get("intent", "content")
    usage: dict = {}
    if prep.get("direct_answer"):
        answer = prep["direct_answer"]          # A 즉답(모델 호출 없음)
    else:
        answer = "".join(run_claude_stream(prep["prompt"], usage_sink=usage))
        # 운영과 동일하게 환각 출처 정제(본문 불변).
        answer, _ = verify_citations(answer, prep.get("sources", []))

    sources = prep.get("sources", [])
    src_strs = _source_strings(sources)

    checks: list[tuple[str, bool, str]] = []
    exp_intent = case.get("expect_intent")
    if exp_intent:
        checks.append(("intent", intent == exp_intent, f"{intent}≟{exp_intent}"))
    for inc in case.get("must_include", []):
        checks.append((f"include:{inc}", inc in answer, ""))
    for exc in case.get("must_not", []):
        checks.append((f"not:{exc}", exc not in answer, ""))
    for cite in case.get("must_cite", []):
        ok = any(cite in s for s in src_strs)
        checks.append((f"cite:{cite}", ok, ""))

    det_pass = all(ok for _, ok, _ in checks)
    judged = None
    if use_judge:
        jp, jr = _judge(case["question"], answer, case)
        judged = {"pass": jp, "reason": jr}
        checks.append(("judge", jp, jr))

    status = "PASS" if all(ok for _, ok, _ in checks) else "FAIL"
    return {
        "id": cid, "status": status, "intent": intent, "checks": checks,
        "usage": usage, "answer_len": len(answer), "sources": src_strs,
        "judged": judged, "reason": "",
    }


def _fmt_report(results: list[dict], use_judge: bool) -> str:
    lines = ["# Golden 평가 리포트", ""]
    lines.append(f"- 케이스 수: {len(results)}")
    npass = sum(1 for r in results if r["status"] == "PASS")
    nfail = sum(1 for r in results if r["status"] == "FAIL")
    nskip = sum(1 for r in results if r["status"] in ("SKIP", "ERROR"))
    lines.append(f"- PASS {npass} / FAIL {nfail} / SKIP·ERROR {nskip}")
    lines.append(f"- LLM 심판: {'켜짐' if use_judge else '꺼짐'}")
    lines.append("")
    lines.append("| id | status | intent | in_tok | out_tok | cache_r | cache_w | 가중비용 |")
    lines.append("|---|---|---|--:|--:|--:|--:|--:|")
    tot = {"in": 0, "out": 0, "cr": 0, "cw": 0, "cost": 0.0}
    for r in results:
        u = r.get("usage", {})
        cost = _est_cost(u)
        tot["in"] += u.get("input_tokens") or 0
        tot["out"] += u.get("output_tokens") or 0
        tot["cr"] += u.get("cache_read") or 0
        tot["cw"] += u.get("cache_write") or 0
        tot["cost"] += cost
        lines.append(
            f"| {r['id']} | {r['status']} | {r.get('intent') or '-'} | "
            f"{u.get('input_tokens') or 0} | {u.get('output_tokens') or 0} | "
            f"{u.get('cache_read') or 0} | {u.get('cache_write') or 0} | {cost:.0f} |"
        )
    lines.append(
        f"| **합계** |  |  | {tot['in']} | {tot['out']} | "
        f"{tot['cr']} | {tot['cw']} | {tot['cost']:.0f} |"
    )
    lines.append("")
    lines.append("## 케이스별 검사 상세")
    for r in results:
        lines.append(f"\n### {r['id']} — {r['status']}")
        if r.get("reason"):
            lines.append(f"- 사유: {r['reason']}")
        for name, ok, note in r.get("checks", []):
            mark = "✅" if ok else "❌"
            lines.append(f"- {mark} {name}" + (f" ({note})" if note else ""))
    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser(description="Golden Q&A 회귀 하네스")
    ap.add_argument("--judge", action="store_true", help="haiku LLM 심판 사용")
    ap.add_argument("--case", help="특정 케이스 id만 실행")
    ap.add_argument("--limit", type=int, default=0, help="앞에서 N개만")
    args = ap.parse_args()

    cases = load_cases()
    if args.case:
        cases = [c for c in cases if c["id"] == args.case]
    if args.limit > 0:
        cases = cases[:args.limit]

    results = []
    for c in cases:
        print(f"[run] {c['id']} ...", flush=True)
        r = run_case(c, args.judge)
        results.append(r)
        det = ", ".join(f"{'OK' if ok else 'X'}:{n}" for n, ok, _ in r["checks"])
        print(f"  -> {r['status']}  {det}", flush=True)

    report = _fmt_report(results, args.judge)
    ts = time.strftime("%Y%m%d_%H%M%S")
    out = REPORT_DIR / f"report_{ts}.md"
    try:
        out.write_text(report, encoding="utf-8")
    except OSError:
        out = None

    print("\n" + "=" * 60)
    npass = sum(1 for r in results if r["status"] == "PASS")
    nfail = sum(1 for r in results if r["status"] == "FAIL")
    nskip = sum(1 for r in results if r["status"] in ("SKIP", "ERROR"))
    print(f"PASS {npass} / FAIL {nfail} / SKIP·ERROR {nskip}")
    if out:
        print(f"리포트: {out}")

    # 실패가 있으면 비정상 종료코드(회귀 게이트로 쓰기 좋게).
    raise SystemExit(1 if nfail else 0)


if __name__ == "__main__":
    main()
