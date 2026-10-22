#!/usr/bin/env python3
"""
운영자가 직접 지정한 댓글의 작성자를 채널에서 차단하는 1회성 스크립트.

사용 예:
  python ban.py --match "그래서 님 몸이 구린거임|말 존나 많네 구질구질" --dry-run
  python ban.py --match "그래서 님 몸이 구린거임|말 존나 많네 구질구질"

- 검토 대기 스레드 + 최근 공개 스레드(대댓글 포함)에서 본문에 패턴(공백 무시)이 포함된 댓글을 찾음
- 찾은 댓글을 "거부" 처리하면서 작성자를 채널에서 숨김 (YouTube API banAuthor)
- 한 패턴에 여러 댓글이 걸리면 안전을 위해 그 패턴은 차단하지 않고 보고만 함
- --dry-run 이면 찾기만 하고 바꾸지 않음
- 차단 결과를 누적 데이터(offender_data.json)에 기록해서 봇이 이어받게 함
- 해제: 스튜디오 → 설정 → 커뮤니티 → "채널에서 숨겨진 사용자"
"""

import argparse
from datetime import datetime

from googleapiclient.errors import HttpError

from modbot import (
    NEVER_HIDE_IDS,
    get_authenticated_service,
    get_my_channel_id,
    get_replies,
    load_data,
    log,
    reject_and_ban_author,
    save_data,
)
from unhide import list_threads, norm


def main():
    parser = argparse.ArgumentParser(description="지정한 댓글 작성자 차단 (1회성)")
    parser.add_argument("--match", required=True, help="차단할 댓글 본문 일부. 여러 개는 | 로 구분")
    parser.add_argument("--max", type=int, default=400, help="상태별로 훑을 최대 스레드 수")
    parser.add_argument("--dry-run", action="store_true", help="찾기만 하고 바꾸지 않음")
    args = parser.parse_args()

    patterns = [(p.strip(), norm(p)) for p in args.match.split("|") if p.strip()]
    if not patterns:
        raise SystemExit("--match 가 비어 있습니다")

    youtube = get_authenticated_service()
    my_channel = get_my_channel_id(youtube)

    candidates: dict[str, dict] = {}
    for status in ("heldForReview", "published"):
        threads = list_threads(youtube, my_channel, status, args.max)
        log.info(f"{status} 스레드 {len(threads)}개 조회")
        for t in threads:
            top = t["snippet"]["topLevelComment"]
            candidates.setdefault(top["id"], {**top, "_where": f"댓글/{status}"})
            if t["snippet"].get("totalReplyCount", 0) > 0:
                for r in get_replies(youtube, top["id"]):
                    candidates.setdefault(r["id"], {**r, "_where": "대댓글"})
    log.info(f"후보 댓글 {len(candidates)}개 수집")

    plan = []
    for raw, pat in patterns:
        hits = []
        for cid, c in candidates.items():
            sn = c.get("snippet", {})
            text = sn.get("textOriginal") or sn.get("textDisplay", "")
            if pat in norm(text):
                hits.append((cid, c, text))
        if not hits:
            log.warning(f"[찾지 못함] \"{raw}\"")
            continue
        if len(hits) > 1:
            log.warning(f"[여러 개 일치 — 건너뜀] \"{raw}\" → {len(hits)}개. 더 구체적인 문구로 다시 지정하세요")
            for cid, c, text in hits:
                log.warning(f"     @{c['snippet'].get('authorDisplayName', '')} \"{text[:60]}\"")
            continue
        cid, c, text = hits[0]
        sn = c["snippet"]
        author_cid = sn.get("authorChannelId", {}).get("value", "")
        if author_cid == my_channel:
            log.warning(f"[운영자 본인 댓글 — 건너뜀] \"{raw}\"")
            continue
        if cid in NEVER_HIDE_IDS:
            log.warning(f"[예외 목록 댓글 — 건너뜀] \"{raw}\"")
            continue
        plan.append((cid, author_cid, sn.get("authorDisplayName", "알 수 없음"), text, c["_where"]))

    log.info(f"차단 대상 {len(plan)}명:")
    for cid, acid, name, text, where in plan:
        log.info(f"  🚫 {name} [{where}] \"{text[:80]}\"")

    if args.dry_run:
        log.info("dry-run 이므로 변경하지 않음")
        return

    data = load_data()
    done = 0
    for cid, acid, name, text, where in plan:
        try:
            reject_and_ban_author(youtube, cid)
        except HttpError as e:
            log.error(f"차단 실패: {name} | {e}")
            continue
        done += 1
        log.info(f"차단 완료: {name}")
        off = data["offenders"].setdefault(acid, {
            "name": name,
            "channel_id": acid,
            "count": 0,
            "last_comment": "",
            "last_reason": "",
            "timestamps": [],
            "severe_count": 0,
            "banned": False,
            "banned_at": None,
            "channel_url": f"https://www.youtube.com/channel/{acid}",
        })
        off["name"] = name
        off["banned"] = True
        off["banned_at"] = datetime.now().isoformat()
        off["ban_reason"] = "운영자 직접 지정"
        off["ban_comment"] = text[:200]
    data["stats"]["total_banned"] = data["stats"].get("total_banned", 0) + done
    save_data(data)
    log.info(f"차단 {done}/{len(plan)}명 완료")


if __name__ == "__main__":
    main()
