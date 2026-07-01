#!/usr/bin/env python3
"""
YouTube 악플 자동 숨기기 봇 (AI 판단 버전)
- Claude Sonnet이 맥락 기반으로 댓글을 판단
- 숨기기(heldForReview)만, 삭제 없음
- 반복 악플러 추적
"""

import os
import json
import time
import logging
from datetime import datetime
from pathlib import Path

import anthropic
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

# ─── 설정 ─────────────────────────────────────────────────────────────────────

SCOPES = ["https://www.googleapis.com/auth/youtube.force-ssl"]
TOKEN_FILE = "token.json"
CREDENTIALS_FILE = "client_secrets.json"
DATA_FILE = "offender_data.json"
LOG_FILE = "modbot.log"

REPEAT_THRESHOLD = 3
AI_BATCH_SIZE = 20

# ──────────────────────────────────────────────────────────────────────────────
# ★ 여기를 수정해서 AI 판단 기준을 바꾸세요 ★
# ──────────────────────────────────────────────────────────────────────────────
MODERATION_PROMPT = """
당신은 유튜브 채널 댓글 분석 전문가입니다.
단순 키워드 매칭이 아니라 댓글 전체의 맥락, 뉘앙스, 의도를 깊이 읽고 판단하세요.

[채널 운영자 정보]
- 직업: 약사이자 유튜버
- 판매 제품: 운동 앱, 코칭 앱, 식단 앱, 운동 강의, 운동 책
- 본인이 영상에 직접 출연함
- 모든 댓글은 이 운영자의 유튜브 영상에 달린 것임

[HIDE 판단 기준]

▶ 주어 판단 원칙 (가장 중요)
- 주어가 없거나 불분명한 비판 → 무조건 운영자를 향한 것으로 간주 → HIDE
- '얘', '이분', '이 사람', '이 유튜버', '채널 주인', '채널 주인장', '주인장', '약사', '약사님' 이 주어인 비판 → HIDE
- 명확히 제3자(다른 트레이너, 타 유튜버, 업계 전반 등)가 주어인 경우만 예외로 OK

▶ 전문성/자격 공격 (키워드 없어도 맥락으로 판단)
- 운영자가 잘 모른다, 틀렸다, 자격이 없다는 뉘앙스의 모든 댓글
- "약사가 뭘 아냐", "약사 주제에", "의사도 아닌데", "약사면서 이것도 모름?" 류
- 신체/외모를 근거로 전문성을 부정하는 댓글
  예: "이 몸으로 강의를?", "몸만 봐도 앎", "직접 해보긴 했냐", "몸 보면 알 수 있음"
- 정보가 틀렸다, 잘못됐다, 위험하다는 류의 댓글
- 운영자 외모, 체형, 몸에 대한 부정적 언급 일체

▶ 영상/콘텐츠 부정 (조금이라도 부정적이면 HIDE)
- 영상 길이: "너무 길다", "줄여라", "핵심만 말해라", "시간 낭비"
- 설명 방식: "설명이 너무 복잡하다", "이해하기 어렵다", "너무 어렵게 설명한다", "복잡하게 설명한다"
- 영상 품질, 편집, 말투, 태도에 대한 부정
- "내용이 별로다", "도움이 안 됐다", "별로네", "실망이다"
- "구독 취소했다", "비추", "다른 채널이 낫다"
- 비꼬는 투의 댓글 (겉으로 칭찬처럼 보여도 맥락상 비꼬는 것)

▶ 사업 및 제품 부정 (조금이라도 부정적이면 HIDE)
- 앱, 강의, 책, 코칭에 대한 부정적 언급
  예: "비싸다", "별로다", "오류 많다", "환불", "사기", "다른 앱이 낫다", "가성비 별로", "가짜 후기"
- 앱의 후기, 광고 방식, 사업 전반의 대한 조금이라도 부정적인 댓글
- 단, 순수 질문은 OK ("앱 언제 나와요?", "가격이 얼마예요?")

[OK 기준 — 살려야 할 댓글]
- 운영자나 제품에 부정적이지 않은 모든 댓글은 OK
- 제3자(다른 트레이너, 타 유튜버, 업계, 일반인 등)를 향한 비판은 OK
  예: "특정 헬스 유튜버가", "트레이너들이", "의사들이" 등 채널 관리자 외의 주체가 주어인 경우
- 비속어가 포함되어 있어도 운영자/제품을 향한 부정이 아니면 OK
  예: "ㅅㅂ", "미쳤다", "죽겠다", "ㄹㅇ" 등은 감탄/공감 표현일 수 있음

[핵심 판단 원칙]
- 키워드가 없어도 전체 맥락을 읽고 운영자나 제품에 부정적인 의도가 느껴지면 → HIDE
- 주어가 불분명하면 → HIDE (운영자를 향한 것으로 간주)
- 운영자, 운영자의 행동, 영상 내용을 비꼬는 뉘앙스가 있으면 → HIDE
- 명백히 제3자를 향하거나 긍정적/중립적인 댓글은 → OK
- 제품이나 운영자에 조금이라도 부정적이면 → HIDE

결과를 반드시 아래 JSON 형식으로만 응답하세요. 다른 텍스트 없이 JSON만:
{
  "results": [
    {"id": 1, "action": "HIDE", "reason": "맥락상 운영자 전문성 부정"},
    {"id": 2, "action": "OK", "reason": "긍정적 맥락의 비속어"},
    ...
  ]
}
"""
# ──────────────────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler()
    ]
)
log = logging.getLogger(__name__)

def get_authenticated_service():
    creds = None
    if Path(TOKEN_FILE).exists():
        creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not Path(CREDENTIALS_FILE).exists():
                print(f"\n❌ '{CREDENTIALS_FILE}' 파일이 없습니다.")
                raise FileNotFoundError(CREDENTIALS_FILE)
            flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_FILE, SCOPES)
            creds = flow.run_local_server(port=0)
        with open(TOKEN_FILE, "w") as f:
            f.write(creds.to_json())
    return build("youtube", "v3", credentials=creds)

def get_anthropic_client():
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise EnvironmentError("ANTHROPIC_API_KEY not set")
    return anthropic.Anthropic(api_key=api_key)

def load_data():
    if Path(DATA_FILE).exists():
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {
        "offenders": {},
        "processed": [],
        "stats": {"total_hidden": 0, "total_scanned": 0, "ai_calls": 0, "last_run": None}
    }

def save_data(data):
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def ai_judge_batch(client: anthropic.Anthropic, comments: list[dict]) -> list[dict]:
    comment_list = "\n".join(
        f'[{c["id"]}] {c["text"][:500]}'
        for c in comments
    )
    user_message = f"아래 댓글들을 판단해주세요:\n\n{comment_list}"

    try:
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=2000,
            system=MODERATION_PROMPT,
            messages=[{"role": "user", "content": user_message}]
        )

        raw = response.content[0].text.strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]

        parsed = json.loads(raw)
        return parsed.get("results", [])

    except json.JSONDecodeError as e:
        log.error(f"AI 응답 파싱 실패: {e}\n응답: {raw[:200]}")
        return []
    except anthropic.APIError as e:
        log.error(f"Anthropic API 오류: {e}")
        return []

def hide_comment(youtube, comment_id: str):
    youtube.comments().setModerationStatus(
        id=comment_id,
        moderationStatus="heldForReview"
    ).execute()

def get_replies(youtube, thread_id: str, is_pinned: bool = False) -> list[dict]:
    """대댓글 가져오기. 고정 댓글이면 is_pinned=True 표시."""
    replies = []
    page_token = None
    try:
        while True:
            params = {
                "part": "snippet",
                "parentId": thread_id,
                "maxResults": 100,
            }
            if page_token:
                params["pageToken"] = page_token
            response = youtube.comments().list(**params).execute()
            for item in response.get("items", []):
                item["_is_reply"] = True
                item["_is_pinned_reply"] = is_pinned
            replies.extend(response.get("items", []))
            page_token = response.get("nextPageToken")
            if not page_token:
                break
    except HttpError as e:
        log.warning(f"대댓글 조회 실패 (thread={thread_id}): {e}")
    return replies

def get_channel_comments(youtube, channel_id: str, max_results: int = 200):
    all_threads = []
    page_token = None
    while True:
        params = {
            "part": "snippet",
            "allThreadsRelatedToChannelId": channel_id,
            "maxResults": min(max_results - len(all_threads), 100),
            "order": "time",
            "moderationStatus": "published",
        }
        if page_token:
            params["pageToken"] = page_token
        response = youtube.commentThreads().list(**params).execute()
        all_threads.extend(response.get("items", []))
        page_token = response.get("nextPageToken")
        if not page_token or len(all_threads) >= max_results:
            break
    return all_threads

def get_video_comments(youtube, video_id: str, max_results: int = 200):
    all_threads = []
    page_token = None
    try:
        video_resp = youtube.videos().list(part="snippet", id=video_id).execute()
        if not video_resp.get("items"):
            raise ValueError(f"영상을 찾을 수 없음: {video_id}")
        title = video_resp["items"][0]["snippet"]["title"]
        log.info(f"영상: \"{title}\"")
    except HttpError as e:
        raise ValueError(f"영상 정보 조회 실패: {e}")

    while True:
        params = {
            "part": "snippet",
            "videoId": video_id,
            "maxResults": min(max_results - len(all_threads), 100),
            "order": "time",
            "moderationStatus": "published",
        }
        if page_token:
            params["pageToken"] = page_token
        response = youtube.commentThreads().list(**params).execute()
        all_threads.extend(response.get("items", []))
        page_token = response.get("nextPageToken")
        if not page_token or len(all_threads) >= max_results:
            break
    return all_threads

def collect_all_comments(youtube, threads: list[dict]) -> list[dict]:
    """
    스레드에서 최상위 댓글 + 대댓글을 모두 수집.
    고정 댓글 대댓글은 맨 앞에 배치 (우선 처리).
    """
    pinned_replies = []
    normal_top = []
    normal_replies = []

    for thread in threads:
        snippet = thread["snippet"]
        is_pinned = snippet.get("isPublic", True) and \
                    thread["snippet"].get("topLevelComment", {}).get("snippet", {}).get("likeCount", 0) == -1
        # 고정 댓글 감지: canReply + replies 수 확인보다 더 정확한 방법
        top_comment = snippet.get("topLevelComment", {})
        top_snippet = top_comment.get("snippet", {})

        # 고정 여부는 별도 필드가 없어서 pinnedCommentId로 감지
        is_pinned = snippet.get("canReply", False) and \
                    top_snippet.get("authorChannelId", {}).get("value", "") != ""

        # 실제 고정 감지: YouTube API에서 pinned comment는 별도 표시 없음
        # 대신 대댓글 수가 있는 스레드를 모두 가져오되 정상 처리
        thread["_is_pinned"] = False  # 기본값

        reply_count = snippet.get("totalReplyCount", 0)
        top_comment["_is_reply"] = False
        top_comment["_is_pinned_reply"] = False
        normal_top.append(top_comment)

        # 대댓글이 있으면 가져오기
        if reply_count > 0:
            thread_id = top_comment["id"]
            replies = get_replies(youtube, thread_id, is_pinned=thread.get("_is_pinned", False))
            if thread.get("_is_pinned"):
                pinned_replies.extend(replies)
            else:
                normal_replies.extend(replies)

    # 순서: 고정댓글 대댓글 → 일반 최상위 댓글 → 일반 대댓글
    return pinned_replies + normal_top + normal_replies

def parse_video_id(video_input: str) -> str:
    import re
    patterns = [
        r"youtu\.be/([A-Za-z0-9_-]{11})",
        r"[?&]v=([A-Za-z0-9_-]{11})",
        r"^([A-Za-z0-9_-]{11})$",
    ]
    for pattern in patterns:
        m = re.search(pattern, video_input)
        if m:
            return m.group(1)
    raise ValueError(f"영상 ID를 파싱할 수 없음: {video_input}")

def get_my_channel_id(youtube):
    response = youtube.channels().list(part="id,snippet", mine=True).execute()
    items = response.get("items", [])
    if not items:
        raise ValueError("채널을 찾을 수 없습니다.")
    ch = items[0]
    log.info(f"채널: {ch['snippet']['title']} ({ch['id']})")
    return ch["id"]

def run_moderation(max_comments: int = 200, video_input: str = None):
    log.info("=" * 50)

    youtube = get_authenticated_service()
    ai = get_anthropic_client()
    data = load_data()

    hidden_count = 0
    scanned_count = 0
    ai_call_count = 0

    try:
        if video_input:
            video_id = parse_video_id(video_input)
            log.info(f"AI 악플 봇 시작 — 영상 모드 (ID: {video_id})")
            threads = get_video_comments(youtube, video_id, max_comments)
        else:
            log.info("AI 악플 봇 시작 — 채널 전체 모드")
            channel_id = get_my_channel_id(youtube)
            threads = get_channel_comments(youtube, channel_id, max_comments)

        log.info(f"스레드 {len(threads)}개 가져옴, 대댓글 수집 중...")

        # 최상위 댓글 + 대댓글 모두 수집 (고정 댓글 대댓글 우선)
        all_comments = collect_all_comments(youtube, threads)
        log.info(f"총 댓글+대댓글 {len(all_comments)}개 수집됨")

        # 이미 처리한 댓글 제외
        new_comments = [
            c for c in all_comments
            if c["id"] not in data["processed"]
        ]
        log.info(f"새 댓글 {len(new_comments)}개 판단 필요")

        for batch_start in range(0, len(new_comments), AI_BATCH_SIZE):
            batch = new_comments[batch_start: batch_start + AI_BATCH_SIZE]

            ai_input = []
            comment_map = {}
            for i, comment in enumerate(batch, 1):
                snippet = comment.get("snippet", {})
                text = snippet.get("textOriginal", snippet.get("textDisplay", ""))
                is_reply = comment.get("_is_reply", False)
                is_pinned_reply = comment.get("_is_pinned_reply", False)
                label = "[대댓글-고정]" if is_pinned_reply else "[대댓글]" if is_reply else "[댓글]"
                ai_input.append({"id": i, "text": f"{label} {text}"})
                comment_map[i] = comment

            scanned_count += len(batch)

            log.info(f"AI 판단 중... ({batch_start+1}~{batch_start+len(batch)}개)")
            results = ai_judge_batch(ai, ai_input)
            ai_call_count += 1

            for result in results:
                idx = result.get("id")
                action = result.get("action", "OK")
                reason = result.get("reason", "")

                if idx not in comment_map:
                    continue

                comment = comment_map[idx]
                comment_id = comment["id"]
                snippet = comment.get("snippet", {})
                text = snippet.get("textOriginal", snippet.get("textDisplay", ""))
                author_name = snippet.get("authorDisplayName", "알 수 없음")
                author_channel_id = snippet.get("authorChannelId", {}).get("value", "unknown")
                is_pinned_reply = comment.get("_is_pinned_reply", False)

                data["processed"].append(comment_id)

                if action == "HIDE":
                    try:
                        hide_comment(youtube, comment_id)
                        hidden_count += 1

                        if author_channel_id not in data["offenders"]:
                            data["offenders"][author_channel_id] = {
                                "name": author_name,
                                "channel_id": author_channel_id,
                                "count": 0,
                                "last_comment": "",
                                "last_reason": "",
                                "timestamps": [],
                                "channel_url": f"https://www.youtube.com/channel/{author_channel_id}"
                            }

                        offender = data["offenders"][author_channel_id]
                        offender["count"] += 1
                        offender["name"] = author_name
                        offender["last_comment"] = text[:100]
                        offender["last_reason"] = reason
                        offender["timestamps"].append(datetime.now().isoformat())

                        tag = "📌대댓글" if is_pinned_reply else "↩대댓글" if comment.get("_is_reply") else "💬댓글"
                        log.info(f"숨김 {tag}: @{author_name} | {reason} | \"{text[:40]}...\"")
                        time.sleep(0.3)

                    except HttpError as e:
                        log.error(f"숨김 실패 ({comment_id}): {e}")

            if batch_start + AI_BATCH_SIZE < len(new_comments):
                time.sleep(1)

        data["stats"]["total_hidden"] += hidden_count
        data["stats"]["total_scanned"] += scanned_count
        data["stats"]["ai_calls"] = data["stats"].get("ai_calls", 0) + ai_call_count
        data["stats"]["last_run"] = datetime.now().isoformat()

        if len(data["processed"]) > 10000:
            data["processed"] = data["processed"][-10000:]

        save_data(data)

        repeat_offenders = {
            cid: info for cid, info in data["offenders"].items()
            if info["count"] >= REPEAT_THRESHOLD
        }

        log.info(f"\n{'='*50}")
        log.info(f"결과: {scanned_count}개 스캔 → {hidden_count}개 숨김 (AI 호출 {ai_call_count}회)")
        log.info(f"누적: 총 {data['stats']['total_hidden']}개 숨김, AI {data['stats']['ai_calls']}회 호출")

        if repeat_offenders:
            log.info(f"\n⚠️  반복 악플러 {len(repeat_offenders)}명:")
            for cid, info in sorted(repeat_offenders.items(), key=lambda x: -x[1]["count"]):
                log.info(f"  → {info['name']} ({info['count']}회) | {info['channel_url']}")
        else:
            log.info("반복 악플러 없음")

    except HttpError as e:
        log.error(f"YouTube API 오류: {e}")
        raise

    return {
        "scanned": scanned_count,
        "hidden": hidden_count,
        "ai_calls": ai_call_count,
        "repeat_offenders": repeat_offenders if "repeat_offenders" in locals() else {}
    }


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="YouTube AI 악플 자동 숨기기 봇")
    parser.add_argument("--max", type=int, default=200, help="최대 스캔 댓글 수 (기본: 200)")
    parser.add_argument("--watch", action="store_true", help="10분마다 자동 반복 실행")
    parser.add_argument("--video", type=str, default=None, help="특정 영상 URL 또는 ID")
    args = parser.parse_args()

    if args.watch:
        log.info("지속 실행 모드: 10분마다 실행")
        while True:
            try:
                run_moderation(args.max, args.video)
            except Exception as e:
                log.error(f"실행 오류: {e}")
            log.info("10분 후 재실행...")
            time.sleep(600)
    else:
        run_moderation(args.max, args.video)
