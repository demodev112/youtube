#!/usr/bin/env python3
"""
숨김(검토 대기) 처리된 댓글을 본문 일부로 찾아 다시 공개하는 1회성 스크립트.

사용 예:
  python unhide.py --match "약사세요?|책도 좀 리뉴얼" --max 400 [--dry-run]

- 채널의 검토 대기·스팸 의심 스레드 + 최근 공개 스레드(대댓글 포함)를 훑어서
  본문에 패턴(공백 무시)이 포함된 댓글을 찾는다.
- --dry-run 이면 찾기만 하고 바꾸지 않는다.
- --hide 를 주면 반대로 찾은 댓글을 숨김(검토 대기)으로 바꾼다 (잘못 복구한 댓글을 되돌릴 때)
- 복구한 댓글 ID를 로그에 남기므로, modbot.py 의 NEVER_HIDE_IDS 에 넣어 재숨김을 막을 수 있다.
- --owner-reply-only: 운영자가 답글을 단 최상위 댓글만 복구한다. 숨긴 댓글의 답글은 숨김 상태에서는
  읽히지 않아서, 공개로 바꾼 직후 답글을 읽고 운영자 답글이 없으면 바로 숨김으로 되돌린다.
"""

import argparse
import time

from googleapiclient.errors import HttpError

from modbot import get_authenticated_service, get_my_channel_id, get_replies, log


# --owner-reply-only: 공개 직후 답글을 다시 읽을 때 기다리는 시간(초)
REPLY_READ_WAITS = (0, 10, 30, 60)


def norm(s: str) -> str:
    return "".join((s or "").split())


def list_threads(youtube, channel_id: str, status: str, max_results: int) -> list[dict]:
    threads, page_token = [], None
    while True:
        params = {
            # replies 를 같이 받으면 숨겨진 대댓글이 스레드에 딸려 옴 (comments.list 로는 안 나옴)
            "part": "snippet,replies",
            "allThreadsRelatedToChannelId": channel_id,
            "maxResults": min(max_results - len(threads), 100),
            "order": "time",
            "moderationStatus": status,
        }
        if page_token:
            params["pageToken"] = page_token
        try:
            resp = youtube.commentThreads().list(**params).execute()
        except HttpError as e:
            log.warning(f"스레드 조회 실패 (status={status}): {e}")
            break
        threads.extend(resp.get("items", []))
        page_token = resp.get("nextPageToken")
        if not page_token or len(threads) >= max_results:
            break
    return threads


def main():
    parser = argparse.ArgumentParser(description="숨긴 댓글 복구 (1회성)")
    parser.add_argument("--match", required=True, help="복구할 댓글 본문 일부. 여러 개는 | 로 구분")
    parser.add_argument("--max", type=int, default=400, help="상태별로 훑을 최대 스레드 수")
    parser.add_argument("--dry-run", action="store_true", help="찾기만 하고 바꾸지 않음")
    parser.add_argument("--hide", action="store_true", help="공개 대신 숨김(검토 대기)으로 바꿈")
    parser.add_argument("--owner-reply-only", action="store_true",
                        help="운영자 답글이 달린 최상위 댓글만 복구 (없으면 즉시 숨김으로 되돌림)")
    args = parser.parse_args()

    patterns = [norm(p) for p in args.match.split("|") if p.strip()]
    if not patterns:
        raise SystemExit("--match 가 비어 있습니다")

    youtube = get_authenticated_service()
    channel_id = get_my_channel_id(youtube)

    candidates: dict[str, tuple[str, str, str]] = {}  # id -> (text, author, where)
    # 최상위 댓글 id -> (답글 수, [(작성자, 본문)]) — 일치한 댓글에 어떤 답글이 달렸는지 보여 주려고 모은다
    thread_replies: dict[str, tuple[int, list[tuple[str, str]]]] = {}
    # likelySpam: YouTube 자체 스팸 필터가 "스팸일 수 있음"으로 빼 둔 댓글
    for status in ("heldForReview", "likelySpam", "published"):
        threads = list_threads(youtube, channel_id, status, args.max)
        log.info(f"{status} 스레드 {len(threads)}개 조회")
        for t in threads:
            top = t["snippet"]["topLevelComment"]
            sn = top.get("snippet", {})
            candidates[top["id"]] = (
                sn.get("textOriginal", sn.get("textDisplay", "")),
                sn.get("authorDisplayName", ""),
                f"댓글/{status}",
            )
            seen_replies: dict[str, tuple[str, str]] = {}
            # 스레드에 딸려 온 대댓글 (숨겨진 대댓글은 여기로만 나옴)
            for r in t.get("replies", {}).get("comments", []):
                rs = r.get("snippet", {})
                seen_replies[r["id"]] = (rs.get("authorDisplayName", ""), rs.get("textOriginal", rs.get("textDisplay", "")))
                candidates.setdefault(r["id"], (
                    rs.get("textOriginal", rs.get("textDisplay", "")),
                    rs.get("authorDisplayName", ""),
                    f"대댓글/{status}스레드",
                ))
            if t["snippet"].get("totalReplyCount", 0) > 0:
                for r in get_replies(youtube, top["id"]):
                    rs = r.get("snippet", {})
                    seen_replies[r["id"]] = (rs.get("authorDisplayName", ""), rs.get("textOriginal", rs.get("textDisplay", "")))
                    candidates[r["id"]] = (
                        rs.get("textOriginal", rs.get("textDisplay", "")),
                        rs.get("authorDisplayName", ""),
                        f"대댓글/{status}스레드",
                    )
            thread_replies[top["id"]] = (t["snippet"].get("totalReplyCount", 0), list(seen_replies.values()))
    log.info(f"후보 댓글 {len(candidates)}개 수집")

    matched = []
    for cid, (text, author, where) in candidates.items():
        hits = [p for p in patterns if p in norm(text)]
        if hits:
            matched.append((cid, text, author, where))

    if not matched:
        log.warning("패턴과 일치하는 댓글이 없습니다")
        return

    log.info(f"일치 {len(matched)}개:")
    for cid, text, author, where in matched:
        log.info(f"  [{where}] id={cid} @{author} | \"{text[:80]}\"")
        if cid in thread_replies:
            count, replies = thread_replies[cid]
            if count or replies:
                log.info(f"      답글 {count}개 (읽힌 것 {len(replies)}개)")
            for r_author, r_text in replies:
                log.info(f"      └ @{r_author} | \"{r_text[:80]}\"")

    if args.dry_run:
        log.info("dry-run 이므로 변경하지 않음")
        return

    if args.owner_reply_only:
        if args.hide:
            raise SystemExit("--owner-reply-only 와 --hide 는 함께 쓸 수 없습니다")
        before = len(matched)
        matched = [m for m in matched if m[0] in thread_replies and thread_replies[m[0]][0] > 0]
        log.info(f"답글이 있는 최상위 댓글만 남김: {before}개 → {len(matched)}개")

    status, verb = ("heldForReview", "숨김") if args.hide else ("published", "복구")
    done = []
    for cid, text, author, where in matched:
        try:
            youtube.comments().setModerationStatus(id=cid, moderationStatus=status).execute()
        except HttpError as e:
            log.error(f"{verb} 실패: id={cid} @{author} | {e}")
            continue
        if args.owner_reply_only:
            # 공개로 바꾼 직후에는 답글이 바로 읽히지 않는다 → 시간을 두고 몇 번 다시 읽는다
            replies = []
            for wait in REPLY_READ_WAITS:
                time.sleep(wait)
                replies = get_replies(youtube, cid)
                if replies:
                    break
                log.info(f"    답글이 아직 안 읽힘 ({wait}초 뒤)")
            owner = [r["snippet"].get("textOriginal", r["snippet"].get("textDisplay", "")) for r in replies
                     if r.get("snippet", {}).get("authorChannelId", {}).get("value") == channel_id]
            for r in replies:
                rs = r.get("snippet", {})
                log.info(f"    └ @{rs.get('authorDisplayName', '')} | \"{rs.get('textOriginal', rs.get('textDisplay', ''))[:120]}\"")
            if not owner:
                youtube.comments().setModerationStatus(id=cid, moderationStatus="heldForReview").execute()
                why = "답글을 끝내 읽지 못함" if not replies else "운영자 답글 없음"
                log.info(f"{why} → 숨김으로 되돌림: id={cid} @{author}")
                continue
        done.append(cid)
        log.info(f"{verb} 완료: id={cid} @{author}")

    log.info(f"{verb} {len(done)}/{len(matched)}개 완료")
    if done and not args.hide:
        log.info("재숨김 방지용 NEVER_HIDE_IDS 에 추가할 ID: " + ", ".join(f'"{c}"' for c in done))


if __name__ == "__main__":
    main()
