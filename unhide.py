#!/usr/bin/env python3
"""
숨김(검토 대기) 처리된 댓글을 본문 일부로 찾아 다시 공개하는 1회성 스크립트.

사용 예:
  python unhide.py --match "약사세요?|책도 좀 리뉴얼" --max 400 [--dry-run]

- 채널의 검토 대기 스레드 + 최근 공개 스레드(대댓글 포함)를 훑어서
  본문에 패턴(공백 무시)이 포함된 댓글을 찾는다.
- --dry-run 이면 찾기만 하고 바꾸지 않는다.
- 복구한 댓글 ID를 로그에 남기므로, modbot.py 의 NEVER_HIDE_IDS 에 넣어 재숨김을 막을 수 있다.
"""

import argparse

from googleapiclient.errors import HttpError

from modbot import get_authenticated_service, get_my_channel_id, get_replies, log


def norm(s: str) -> str:
    return "".join((s or "").split())


def list_threads(youtube, channel_id: str, status: str, max_results: int) -> list[dict]:
    threads, page_token = [], None
    while True:
        params = {
            "part": "snippet",
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
    args = parser.parse_args()

    patterns = [norm(p) for p in args.match.split("|") if p.strip()]
    if not patterns:
        raise SystemExit("--match 가 비어 있습니다")

    youtube = get_authenticated_service()
    channel_id = get_my_channel_id(youtube)

    candidates: dict[str, tuple[str, str, str]] = {}  # id -> (text, author, where)
    for status in ("heldForReview", "published"):
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
            if t["snippet"].get("totalReplyCount", 0) > 0:
                for r in get_replies(youtube, top["id"]):
                    rs = r.get("snippet", {})
                    candidates[r["id"]] = (
                        rs.get("textOriginal", rs.get("textDisplay", "")),
                        rs.get("authorDisplayName", ""),
                        f"대댓글/{status}스레드",
                    )
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

    if args.dry_run:
        log.info("dry-run 이므로 변경하지 않음")
        return

    done = []
    for cid, text, author, where in matched:
        try:
            youtube.comments().setModerationStatus(id=cid, moderationStatus="published").execute()
            done.append(cid)
            log.info(f"복구 완료: id={cid} @{author}")
        except HttpError as e:
            log.error(f"복구 실패: id={cid} @{author} | {e}")

    log.info(f"복구 {len(done)}/{len(matched)}개 완료")
    if done:
        log.info("재숨김 방지용 NEVER_HIDE_IDS 에 추가할 ID: " + ", ".join(f'"{c}"' for c in done))


if __name__ == "__main__":
    main()
