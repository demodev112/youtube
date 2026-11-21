#!/usr/bin/env python3
"""
1회성: 운영자 답글 본문 수정 · 여러 댓글에 같은 운영자 답글 달기.

사용 예:
  python reply.py edit --target <운영자 댓글 ID> --text "..."
  python reply.py reply --parents <최상위 댓글 ID>,<최상위 댓글 ID> --text "..." [--dry-run]

- edit: 운영자 본인 댓글만 수정한다. 수정 후 시간을 두고 다시 읽어 본문이 같은지 확인
- reply: 각 최상위 댓글 아래에 운영자 답글을 단다.
  · 대댓글 ID(점 포함)를 주면 그 대댓글이 달린 최상위 댓글에 단다 (YouTube 는 최상위 댓글에만 답글을 달 수 있음)
  · 같은 본문의 운영자 답글이 이미 있으면 다시 달지 않는다
  · 원댓글이 공개 상태가 아니면(숨김·삭제) 답글을 달아도 아무도 못 보므로 달지 않고 알려 준다
  · --dry-run 이면 원댓글과 기존 답글만 보여 주고 아무것도 달지 않는다
"""

import argparse
import time

from modbot import get_authenticated_service, get_my_channel_id, get_replies, log

READ_WAITS = (0, 10, 30, 60)


def get_comment(youtube, cid: str) -> dict | None:
    """comments.list(id=…) 는 공개된 댓글만 돌려준다."""
    items = youtube.comments().list(part="snippet", id=cid, textFormat="plainText").execute().get("items", [])
    return items[0] if items else None


def text_of(c: dict) -> str:
    sn = c.get("snippet", {})
    return sn.get("textOriginal", sn.get("textDisplay", ""))


def is_owner(c: dict, channel_id: str) -> bool:
    return c.get("snippet", {}).get("authorChannelId", {}).get("value") == channel_id


def read_back(youtube, cid: str, expected: str, label: str) -> bool:
    for wait in READ_WAITS:
        time.sleep(wait)
        now = get_comment(youtube, cid)
        got = text_of(now) if now else None
        log.info(f"{label} ({wait}초 뒤 읽음): {got!r}")
        if got == expected:
            return True
    return False


def edit(youtube, channel_id: str, target: str, text: str):
    dst = get_comment(youtube, target)
    if not dst:
        raise SystemExit(f"댓글을 찾지 못함(공개 상태가 아니거나 삭제됨): {target}")
    if not is_owner(dst, channel_id):
        raise SystemExit(f"운영자 본인 댓글이 아니라서 수정하지 않음: {target}")
    log.info(f"대상 {target} 현재: {text_of(dst)!r}")
    if text_of(dst) == text:
        log.info("이미 같은 본문 — 수정하지 않음")
        return
    youtube.comments().update(part="snippet", body={"id": target, "snippet": {"textOriginal": text}}).execute()
    if read_back(youtube, target, text, f"대상 {target} 수정 후"):
        log.info("수정 완료: 요청한 본문과 글자 하나까지 같음")
    else:
        raise SystemExit("수정 요청은 성공했지만 다시 읽은 본문이 요청과 다름 — 확인 필요")


def reply(youtube, channel_id: str, parents: list[str], text: str, dry_run: bool):
    top_ids = list(dict.fromkeys(p.split(".")[0] for p in parents))
    if len(top_ids) != len(parents):
        log.info(f"대댓글 ID 는 최상위 댓글로 바꿈 · 중복 제거: {len(parents)}개 → {len(top_ids)}개")
    posted, skipped = [], []
    for pid in top_ids:
        parent = get_comment(youtube, pid)
        if not parent:
            log.warning(f"건너뜀 — 원댓글이 공개 상태가 아님(숨김·삭제): {pid}")
            skipped.append(pid)
            continue
        sn = parent["snippet"]
        log.info(f"■ {pid} @{sn.get('authorDisplayName', '')} | {text_of(parent)[:120]!r}")
        replies = get_replies(youtube, pid)
        for r in replies:
            log.info(f"    └ @{r['snippet'].get('authorDisplayName', '')} | {text_of(r)[:100]!r}")
        if any(is_owner(r, channel_id) and text_of(r) == text for r in replies):
            log.info("    같은 본문의 운영자 답글이 이미 있음 — 달지 않음")
            skipped.append(pid)
            continue
        if dry_run:
            continue
        resp = youtube.comments().insert(part="snippet", body={"snippet": {"parentId": pid, "textOriginal": text}}).execute()
        new_id = resp["id"]
        if read_back(youtube, new_id, text, f"    새 답글 {new_id}"):
            log.info(f"    답글 작성 완료: {new_id}")
            posted.append(new_id)
        else:
            log.error(f"    답글 작성 요청은 성공했지만 다시 읽은 본문이 다름 — 확인 필요: {new_id}")
        time.sleep(1)
    if dry_run:
        log.info("dry-run 이므로 답글을 달지 않음")
    log.info(f"답글 작성 {len(posted)}개, 건너뜀 {len(skipped)}개")


def main():
    parser = argparse.ArgumentParser(description="운영자 답글 수정 · 같은 답글 여러 곳에 달기 (1회성)")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_ed = sub.add_parser("edit")
    p_ed.add_argument("--target", required=True)
    p_ed.add_argument("--text", required=True)
    p_re = sub.add_parser("reply")
    p_re.add_argument("--parents", required=True, help="최상위 댓글 ID, 쉼표로 구분")
    p_re.add_argument("--text", required=True)
    p_re.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    text = args.text.strip()
    if not text:
        raise SystemExit("--text 가 비어 있음")
    youtube = get_authenticated_service()
    channel_id = get_my_channel_id(youtube)
    if args.cmd == "edit":
        edit(youtube, channel_id, args.target.strip(), text)
    else:
        parents = [p.strip() for p in args.parents.split(",") if p.strip()]
        reply(youtube, channel_id, parents, text, args.dry_run)


if __name__ == "__main__":
    main()
