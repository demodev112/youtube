#!/usr/bin/env python3
"""
읽기 전용: 채널의 모든 쇼츠를 조회수 순으로 나열. 아무것도 바꾸지 않음.

사용 예:
  python shorts.py

쇼츠 판별:
- YouTube API 는 영상이 쇼츠인지 직접 알려주지 않음
- 1차: 채널의 쇼츠 전용 재생목록(UUSH + 채널 ID 뒷부분)에 들어 있는 영상
- 교차 확인: 길이 3분 이하인데 그 목록에 없는 영상, 그 목록에 있는데 3분 넘는 영상은
  youtube.com/shorts/<ID> 주소가 쇼츠로 열리는지(200) 롱폼으로 넘어가는지(303) 직접 확인해서 따로 보고
"""

import http.client
import re

from googleapiclient.errors import HttpError

from modbot import get_authenticated_service, get_my_channel_id, log


def playlist_video_ids(youtube, playlist_id: str) -> list[str]:
    ids, token = [], None
    while True:
        params = {"part": "contentDetails", "playlistId": playlist_id, "maxResults": 50}
        if token:
            params["pageToken"] = token
        resp = youtube.playlistItems().list(**params).execute()
        ids += [it["contentDetails"]["videoId"] for it in resp.get("items", [])]
        token = resp.get("nextPageToken")
        if not token:
            return ids


def video_meta(youtube, ids: list[str]) -> dict[str, dict]:
    meta = {}
    for i in range(0, len(ids), 50):
        resp = youtube.videos().list(part="snippet,statistics,contentDetails,status",
                                     id=",".join(ids[i:i + 50])).execute()
        meta.update({v["id"]: v for v in resp.get("items", [])})
    return meta


def seconds(duration: str) -> int:
    m = re.fullmatch(r"P(?:(\d+)D)?T?(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", duration or "")
    return sum(int(x or 0) * k for x, k in zip(m.groups(), (86400, 3600, 60, 1))) if m else -1


def shorts_url_status(video_id: str) -> str:
    """youtube.com/shorts/<ID> 응답: 200 이면 쇼츠, 303 이면 롱폼으로 넘어감."""
    try:
        conn = http.client.HTTPSConnection("www.youtube.com", timeout=15)
        conn.request("HEAD", f"/shorts/{video_id}", headers={"User-Agent": "Mozilla/5.0"})
        r = conn.getresponse()
        loc = r.getheader("Location", "")
        conn.close()
        if r.status == 200:
            return "쇼츠(200)"
        if r.status in (301, 302, 303) and "/watch" in loc:
            return f"롱폼({r.status})"
        return f"확인불가({r.status} {loc[:60]})"
    except Exception as e:
        return f"확인불가({e})"


def main():
    youtube = get_authenticated_service()
    channel_id = get_my_channel_id(youtube)
    uploads = youtube.channels().list(part="contentDetails", id=channel_id).execute()[
        "items"][0]["contentDetails"]["relatedPlaylists"]["uploads"]

    all_ids = playlist_video_ids(youtube, uploads)
    meta = video_meta(youtube, all_ids)
    log.info(f"업로드 목록 영상 {len(all_ids)}개, 상세 정보 조회 {len(meta)}개")

    shorts_playlist = "UUSH" + channel_id[2:]
    try:
        shorts_ids = playlist_video_ids(youtube, shorts_playlist)
        log.info(f"쇼츠 전용 목록({shorts_playlist}) 영상 {len(shorts_ids)}개")
    except HttpError as e:
        shorts_ids = []
        log.warning(f"쇼츠 전용 목록 조회 실패 — 길이와 주소 확인으로만 판별: {e}")
    in_list = set(shorts_ids)
    missing_meta = [v for v in shorts_ids if v not in meta]
    if missing_meta:
        log.warning(f"쇼츠 목록에는 있는데 업로드 목록에 없는 영상 {len(missing_meta)}개: {missing_meta}")
        meta.update(video_meta(youtube, missing_meta))

    shorts, notes = set(in_list), []
    for vid, m in meta.items():
        sec = seconds(m["contentDetails"].get("duration", ""))
        title = f'{m["status"].get("privacyStatus")} | {m["snippet"]["title"]}'
        if vid in in_list and sec > 180:
            status = shorts_url_status(vid)
            notes.append(f"쇼츠 목록에 있는데 {sec}초: {vid} | 주소 확인 {status} | {title}")
        elif vid not in in_list and 0 <= sec <= 180:
            status = shorts_url_status(vid)
            notes.append(f"쇼츠 목록에 없는데 {sec}초: {vid} | 주소 확인 {status} | {title}")
            if status.startswith("쇼츠") or (not shorts_ids and not status.startswith("롱폼")):
                shorts.add(vid)

    rows = []
    for vid in shorts:
        m = meta.get(vid)
        if not m:
            continue
        st = m.get("statistics", {})
        rows.append({
            "id": vid,
            "views": int(st.get("viewCount", 0)),
            "likes": st.get("likeCount", "-"),
            "comments": st.get("commentCount", "-"),
            "published": m["snippet"].get("publishedAt", "")[:10],
            "privacy": m["status"].get("privacyStatus"),
            "sec": seconds(m["contentDetails"].get("duration", "")),
            "title": m["snippet"]["title"],
        })
    rows.sort(key=lambda r: (-r["views"], r["published"]))

    log.info(f"■ 쇼츠 {len(rows)}개, 조회수 합계 {sum(r['views'] for r in rows):,}")
    for i, r in enumerate(rows, 1):
        log.info(f"SHORT\t{i}\t{r['views']}\t{r['likes']}\t{r['comments']}\t{r['published']}\t"
                 f"{r['privacy']}\t{r['sec']}\t{r['id']}\t{r['title']}")
    log.info(f"■ 판별 확인이 필요했던 영상 {len(notes)}개")
    for n in notes:
        log.info(f"  {n}")


if __name__ == "__main__":
    main()
