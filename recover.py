#!/usr/bin/env python3
"""
1회성: 숨김(검토 대기) 목록에도, 공개 목록에도 안 나오는 댓글을
봇의 처리 기록(offender_data.json 의 processed)에서 ID 를 찾아 다시 공개하는 스크립트.
(스튜디오에서 삭제 처리된 댓글 등은 목록 조회로는 안 나오고 ID 로만 상태를 바꿀 수 있음)

사용 예:
  python recover.py --anchor UgzncLXS1jmJLf6MIPF4AaABAg --author @dogkingjedog --dry-run
  python recover.py --anchor UgzncLXS1jmJLf6MIPF4AaABAg --author @dogkingjedog

- anchor: 찾는 댓글과 같은 실행에서 처리된 다른 댓글 ID. 처리 기록에서 그 앞뒤 --window 개를 후보로 봄
- 후보 중 지금 공개 상태도, 검토 대기 상태도 아닌 ID 만 남김 (= 목록에서 사라진 댓글)
- 하나씩 공개로 바꾼 뒤 작성자를 확인. 작성자가 --author 와 다르면 바로 검토 대기로 되돌림
- 작성자가 직접 지운 댓글은 되살릴 수 없음 (API 가 찾지 못함으로 응답)
"""

import argparse

from googleapiclient.errors import HttpError

from backfill import fetch_by_ids
from modbot import get_authenticated_service, get_my_channel_id, load_data, log
from unhide import list_threads


def main():
    parser = argparse.ArgumentParser(description="목록에서 사라진 댓글을 처리 기록으로 찾아 복구 (1회성)")
    parser.add_argument("--anchor", required=True, help="같은 실행에서 처리된 다른 댓글 ID")
    parser.add_argument("--author", required=True, help="복구할 댓글의 작성자 표시 이름 (예: @abc)")
    parser.add_argument("--window", type=int, default=30, help="anchor 앞뒤로 살펴볼 처리 기록 개수")
    parser.add_argument("--dry-run", action="store_true", help="후보만 찾고 바꾸지 않음")
    args = parser.parse_args()

    processed = load_data().get("processed", [])
    if args.anchor not in processed:
        raise SystemExit(f"처리 기록에 anchor 가 없습니다: {args.anchor}")
    i = processed.index(args.anchor)
    window = [cid for cid in processed[max(0, i - args.window): i + args.window + 1] if cid != args.anchor]
    log.info(f"처리 기록 {len(processed)}개 중 anchor 위치 {i}, 후보 {len(window)}개")

    youtube = get_authenticated_service()
    my_channel = get_my_channel_id(youtube)

    visible = fetch_by_ids(youtube, window)
    held = set()
    for t in list_threads(youtube, my_channel, "heldForReview", 1000):
        held.add(t["snippet"]["topLevelComment"]["id"])
        for r in t.get("replies", {}).get("comments", []):
            held.add(r["id"])
    gone = [cid for cid in window if cid not in visible and cid not in held]
    log.info(f"공개 {len(visible)}개, 검토 대기 {len([c for c in window if c in held])}개 → 목록에서 사라진 후보 {len(gone)}개")
    for cid in gone:
        log.info(f"  후보 id={cid}")

    if args.dry_run or not gone:
        log.info("dry-run 이므로 변경하지 않음" if args.dry_run else "후보가 없습니다")
        return

    for cid in gone:
        try:
            youtube.comments().setModerationStatus(id=cid, moderationStatus="published").execute()
        except HttpError as e:
            log.warning(f"공개로 변경 실패 (삭제됐을 가능성): id={cid} | {e}")
            continue
        now = fetch_by_ids(youtube, [cid]).get(cid)
        if not now:
            log.warning(f"공개로 바꿨지만 조회되지 않음: id={cid}")
            continue
        sn = now["snippet"]
        author = sn.get("authorDisplayName", "")
        text = sn.get("textOriginal") or sn.get("textDisplay", "")
        if author != args.author:
            youtube.comments().setModerationStatus(id=cid, moderationStatus="heldForReview").execute()
            log.info(f"작성자 다름 → 검토 대기로 되돌림: id={cid} {author} | \"{text[:60]}\"")
            continue
        log.info(f"복구 완료: id={cid} {author} | \"{text[:80]}\"")
        log.info(f"재숨김 방지용 NEVER_HIDE_IDS 에 추가할 ID: \"{cid}\"")
        return
    log.warning(f"{args.author} 의 댓글을 복구하지 못했습니다")


if __name__ == "__main__":
    main()
