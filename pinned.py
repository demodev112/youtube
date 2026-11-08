#!/usr/bin/env python3
"""
1회성: 운영자 본인 댓글(고정 댓글 등)의 본문을 확인하고, 다른 운영자 댓글의 본문을 그대로 복사해 수정하는 스크립트.

사용 예:
  python pinned.py inspect --videos 4M6ZT284HXQ,_EyS30r10Ng
  python pinned.py apply --source <원본 댓글 ID> --target <수정할 댓글 ID>

- inspect: 지정한 영상들 + 최근 업로드 영상들의 운영자 본인 최상위 댓글을 원문 그대로(repr) 출력. 아무것도 바꾸지 않음
- apply: source 댓글 본문(textOriginal)을 글자 하나 바꾸지 않고 target 댓글에 덮어씀. 수정 후 다시 읽어서 같은지 확인
- YouTube API 는 댓글 고정·고정 해제를 지원하지 않고, 어떤 댓글이 고정인지도 알려주지 않음.
  그래서 고정 여부는 스튜디오에서 직접 확인해야 함 (관련성 순 첫 댓글을 참고용으로만 출력)
"""

import argparse
import hashlib

from googleapiclient.errors import HttpError

from modbot import get_authenticated_service, get_my_channel_id, log, parse_video_id


def operator_threads(youtube, video_id: str, channel_id: str) -> list[dict]:
    """영상의 공개 최상위 댓글을 전부 훑어서 운영자 본인 댓글만 반환."""
    found, token = [], None
    while True:
        params = {"part": "snippet", "videoId": video_id, "maxResults": 100,
                  "order": "time", "textFormat": "plainText"}
        if token:
            params["pageToken"] = token
        resp = youtube.commentThreads().list(**params).execute()
        for t in resp.get("items", []):
            top = t["snippet"]["topLevelComment"]
            if top["snippet"].get("authorChannelId", {}).get("value") == channel_id:
                found.append(top)
        token = resp.get("nextPageToken")
        if not token:
            break
    return found


def first_by_relevance(youtube, video_id: str) -> str:
    try:
        resp = youtube.commentThreads().list(part="snippet", videoId=video_id, maxResults=1,
                                             order="relevance", textFormat="plainText").execute()
    except HttpError as e:
        return f"(조회 실패: {e})"
    items = resp.get("items", [])
    return items[0]["snippet"]["topLevelComment"]["id"] if items else "(댓글 없음)"


def show_comment(c: dict):
    sn = c["snippet"]
    text = sn.get("textOriginal", "")
    log.info(f"    id={c['id']} 작성 {sn.get('publishedAt')} 수정 {sn.get('updatedAt')} 좋아요 {sn.get('likeCount')}")
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
    log.info(f"    본문 길이 {len(text)}자, 줄 수 {text.count(chr(10)) + 1}, 지문 {digest} (지문이 같으면 글자 하나까지 같은 본문)")
    log.info(f"    원문(repr): {text!r}")


def show_video(youtube, video_id: str, channel_id: str, title: str = ""):
    log.info(f"■ 영상 {video_id} {title}")
    comments = operator_threads(youtube, video_id, channel_id)
    log.info(f"  운영자 본인 최상위 댓글 {len(comments)}개 (관련성 순 첫 댓글: {first_by_relevance(youtube, video_id)})")
    for c in comments:
        show_comment(c)


def inspect(youtube, channel_id: str, videos: list[str]):
    for v in videos:
        vid = parse_video_id(v) if ("/" in v or "=" in v) else v
        info = youtube.videos().list(part="snippet", id=vid).execute().get("items", [])
        show_video(youtube, vid, channel_id, info[0]["snippet"]["title"] if info else "(영상 정보 없음)")

    ch = youtube.channels().list(part="contentDetails", mine=True).execute()["items"][0]
    uploads = ch["contentDetails"]["relatedPlaylists"]["uploads"]
    items = youtube.playlistItems().list(part="snippet,contentDetails", playlistId=uploads,
                                         maxResults=30).execute().get("items", [])
    ids = [it["contentDetails"]["videoId"] for it in items]
    meta = {v["id"]: v for v in youtube.videos().list(part="snippet,status,contentDetails",
                                                      id=",".join(ids)).execute().get("items", [])}
    log.info("■ 최근 업로드 (업로드 목록 순서 그대로)")
    for vid in ids:
        m = meta.get(vid, {})
        st = m.get("status", {})
        log.info(f"  {vid} | 공개 {st.get('privacyStatus')} 예약 {st.get('publishAt')} | "
                 f"게시 {m.get('snippet', {}).get('publishedAt')} | 길이 {m.get('contentDetails', {}).get('duration')} | "
                 f"{m.get('snippet', {}).get('title')}")
    # 운영자 댓글을 보여줄 영상: 가장 최근 업로드 3개 + 가장 최근 공개 롱폼(3분 초과) 1개
    def seconds(vid):
        import re
        d = meta.get(vid, {}).get("contentDetails", {}).get("duration", "")
        m = re.fullmatch(r"P(?:\d+D)?T?(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", d or "")
        return sum(int(x or 0) * k for x, k in zip(m.groups(), (3600, 60, 1))) if m else 0
    longform = [vid for vid in ids if meta.get(vid, {}).get("status", {}).get("privacyStatus") == "public"
                and seconds(vid) > 180]
    for vid in list(dict.fromkeys(ids[:3] + longform[:1])):
        show_video(youtube, vid, channel_id, meta[vid]["snippet"]["title"])


def get_comment(youtube, cid: str) -> dict | None:
    items = youtube.comments().list(part="snippet", id=cid, textFormat="plainText").execute().get("items", [])
    return items[0] if items else None


def apply(youtube, channel_id: str, source: str, target: str):
    src, dst = get_comment(youtube, source), get_comment(youtube, target)
    if not src or not dst:
        raise SystemExit(f"댓글을 찾지 못함: source={bool(src)} target={bool(dst)}")
    for name, c in (("원본", src), ("대상", dst)):
        if c["snippet"].get("authorChannelId", {}).get("value") != channel_id:
            raise SystemExit(f"{name} 댓글이 운영자 본인 댓글이 아님: {c['id']}")
    text = src["snippet"]["textOriginal"]
    log.info(f"원본 {source}: {text!r}")
    log.info(f"대상 {target} 현재: {dst['snippet']['textOriginal']!r}")
    youtube.comments().update(part="snippet", body={"id": target, "snippet": {"textOriginal": text}}).execute()
    after = get_comment(youtube, target)["snippet"]["textOriginal"]
    log.info(f"대상 {target} 수정 후: {after!r}")
    if after == text:
        log.info("수정 완료: 원본과 글자 하나까지 같음")
    else:
        raise SystemExit("수정은 됐지만 원본과 다름 — 위 두 줄을 비교해서 확인 필요")


def main():
    parser = argparse.ArgumentParser(description="운영자 댓글 확인·본문 복사 (1회성)")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_in = sub.add_parser("inspect")
    p_in.add_argument("--videos", required=True, help="영상 ID 또는 URL, 쉼표로 구분")
    p_ap = sub.add_parser("apply")
    p_ap.add_argument("--source", required=True)
    p_ap.add_argument("--target", required=True)
    args = parser.parse_args()

    youtube = get_authenticated_service()
    channel_id = get_my_channel_id(youtube)
    if args.cmd == "inspect":
        inspect(youtube, channel_id, [v.strip() for v in args.videos.split(",") if v.strip()])
    else:
        apply(youtube, channel_id, args.source, args.target)


if __name__ == "__main__":
    main()
