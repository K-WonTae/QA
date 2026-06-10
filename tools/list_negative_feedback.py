# tools/list_negative_feedback.py
"""
👎(부정) 피드백을 질문·답변·사용 출처·코멘트와 함께 출력한다(F-3 export).
평가셋(②, tests/eval/golden.jsonl) 시드 후보를 추리는 용도.

사용:
  python -m tools.list_negative_feedback            # 표 형태로 출력
  python -m tools.list_negative_feedback --json     # JSONL 로 출력(가공용)
  python -m tools.list_negative_feedback --limit 50

운영 DB를 '읽기만' 한다(쓰기 없음).
"""
import argparse
import json

from app.database import list_negative_feedback


def main() -> None:
    ap = argparse.ArgumentParser(description="👎 피드백 목록")
    ap.add_argument("--limit", type=int, default=500)
    ap.add_argument("--json", action="store_true", help="JSONL 로 출력")
    args = ap.parse_args()

    rows = list_negative_feedback(args.limit)
    if args.json:
        for r in rows:
            print(json.dumps(r, ensure_ascii=False))
        return

    if not rows:
        print("👎 피드백이 없습니다.")
        return

    print(f"👎 피드백 {len(rows)}건 (최근순)\n" + "=" * 60)
    for r in rows:
        print(f"[msg #{r['message_id']}] {r['created_at']}")
        print(f"  질문 : {r['question']}")
        ans = (r["answer"] or "").strip().replace("\n", " ")
        if len(ans) > 200:
            ans = ans[:200] + "…"
        print(f"  답변 : {ans}")
        if r["files"]:
            print(f"  출처 : {', '.join(r['files'])}")
        if r["comment"]:
            print(f"  코멘트: {r['comment']}")
        print("-" * 60)


if __name__ == "__main__":
    main()
