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
VIDEO_DESC_MAX_CHARS = 300   # AI에 넘길 영상 설명 길이
PARENT_TEXT_MAX_CHARS = 300  # AI에 넘길 원댓글 길이

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

[입력 형식]
댓글마다 아래 정보가 함께 주어집니다. 반드시 이 정보를 활용해서 "누구를 향한 말인지" 판단하세요.
- 영상 제목 / 영상 설명: 이 댓글이 달린 영상. 영상 주제와 영상 속 등장인물(운영자 외 제3자)을 파악하는 근거
- 원댓글: 대댓글인 경우, 이 대댓글이 답하고 있는 원래 댓글. 대댓글의 대상이 원댓글 작성자인지 운영자인지 판단하는 근거
- 댓글: 판단 대상 댓글 본문

[1단계: 대상(target) 판단 — 가장 중요, 반드시 먼저 수행]
댓글의 부정적 내용이 누구를 향하는지 아래 중 하나로 정하세요.
- "운영자": 운영자 본인(몸, 외모, 전문성, 말투, 태도, 행동 포함)
- "제품": 운영자의 앱, 강의, 책, 코칭, 광고, 후기, 사업 방식
- "영상": 운영자가 만든 영상 자체(길이, 편집, 설명 방식, 내용, 품질)
- "영상속인물": 영상에서 다루는/등장하는 운영자가 아닌 제3자 (예: 영상이 비판하는 트레이너, 인터뷰 대상, 사례 속 인물, 유명인)
- "다른댓글러": 다른 댓글 작성자 (대댓글에서 원댓글 작성자를 공격하는 경우 등)
- "일반/업계": 특정인이 아닌 일반론, 업계 전반, 의사들, 트레이너들, 헬스장 등
- "본인": 댓글 작성자 자기 자신 (자조, 신세한탄)
- "없음": 부정적 내용 자체가 없는 댓글

대상 판단 규칙:
- 영상 제목/설명을 보고 영상이 제3자를 다루는 영상이면, "얘", "이 사람", "저 사람", "쟤" 같은 지시어는
  그 제3자를 가리킬 가능성을 반드시 고려하세요. 영상 주제인 제3자를 욕하는 댓글은 "영상속인물"입니다.
  예: 영상 제목이 "○○ 트레이너 루틴 분석"일 때 "얘 완전 사기꾼이네" → 영상속인물 → OK
- 영상이 운영자 혼자 나오는 일반 정보 영상이면, "얘", "이 사람", "이분", "이 유튜버", "약사" 등은 운영자입니다.
- 대댓글은 원댓글을 먼저 읽으세요. 원댓글 작성자의 의견을 반박하거나 원댓글 작성자를 비난하는 대댓글은 "다른댓글러"입니다.
  단, 원댓글이 운영자를 옹호하는 글이고 대댓글이 그 옹호를 반박하면서 운영자를 깎아내리면 "운영자"입니다.
- 댓글 내용이 영상 속 운영자의 주장/정보/몸/행동을 평가하는 것이면 "운영자" 또는 "영상"입니다.
- 주어가 없거나 불분명하고, 영상 맥락상 운영자를 향한 것일 가능성이 있으면 "운영자"로 봅니다. (100% 확실할 필요 없음)
- 그러나 영상 맥락상 제3자를 향한 것이 더 자연스러우면 "영상속인물"로 봅니다.

[2단계: HIDE / OK 결정]

★ 최우선 (대상과 무관하게 무조건 HIDE): "AI"라고 지적하는 댓글 — 절대 놓치면 안 됨, 최대한 넓게
- 운영자 본인, 운영자의 몸, 얼굴, 목소리, 말투, 영상, 썸네일, 대본, 자막 등
  무엇이든 AI로 만들었다 / AI 같다 / AI 아니냐 고 말하거나 암시하는 댓글은 전부 HIDE
- 표현 예시 (이 외에도 같은 뜻이면 모두 포함):
  "ai네", "ai임", "ai 아님?", "ai 같은데", "영상 ai", "이 사람 ai", "몸 ai",
  "AI 생성", "에이아이", "인공지능", "딥페이크", "합성", "CG", "생성형",
  "가짜 영상", "실제 사람 맞음?", "진짜 사람임?", "사람이 아닌 것 같다",
  "소라", "미드저니", "veo", "sora", "ai 티 난다", "ai 냄새"
- 농담, 질문, 의심, 반어, 칭찬처럼 보이는 말투("ai인 줄", "ai급 몸")도 전부 HIDE
- 주어가 없거나 불분명해도 AI 언급이 있으면 HIDE
- 짧은 댓글("ai", "AI?", "ㅇㅇ ai")도 HIDE
- 영상 속 제3자가 AI라는 댓글도 HIDE (영상 자체가 AI라는 뉘앙스로 번질 수 있음)
- 예외는 딱 하나: 운영자/영상과 전혀 무관하게 AI를 도구로 언급하는 경우만 OK
  예: "운동 앱에 AI 기능 있나요?", "챗GPT한테 물어봤더니"
  → 이 예외에 해당하는지 조금이라도 애매하면 HIDE

▶ 대상이 "운영자", "제품", "영상" 중 하나이고 부정적이면 → HIDE (강도와 무관, 조금이라도 부정적이면)
- 운영자: 욕설, 조롱, 비하, 인신공격, 몸/외모/체형 지적, 전문성/자격 공격
  예: "이 몸으로 강의를?", "몸만 봐도 앎", "약사가 뭘 아냐", "의사도 아닌데", "틀린 정보 퍼뜨린다"
- 제품: 불만, 비판, 부정적 후기 전부
  예: "비싸다", "별로다", "오류 많다", "환불했다", "사기", "다른 앱이 낫다", "가짜 후기", "광고 그만"
  단, 순수 질문은 OK ("앱 언제 나와요?", "가격이 얼마예요?")
- 영상: 불만, 비판 전부
  예: "너무 길다", "줄여라", "핵심만 말해라", "설명이 복잡하다", "이해 안 된다", "별로네", "실망", "구독 취소", "비추"
- 비꼬는 댓글 (겉으로 칭찬처럼 보여도 맥락상 비꼬는 것) → HIDE
- 정중한 말투여도 불만/비판이면 HIDE ("조금만 짧으면 좋겠어요"도 HIDE)

▶ 대상이 "영상속인물", "다른댓글러", "일반/업계", "본인", "없음" → OK
- 영상이 다루는 제3자를 욕하는 댓글 → OK (운영자를 향한 게 아님)
- 다른 댓글러와 싸우는 댓글 → OK (단, 그 과정에서 운영자/제품/영상도 깎아내리면 HIDE)
- 업계 전반, 트레이너들, 의사들 등 일반론 비판 → OK
- 단순 비속어, 감탄, 공감 표현 → OK
  예: "ㅅㅂ 개힘들다", "미쳤다", "죽겠다", "ㄹㅇ", "개웃기네"
- 긍정적/중립적 댓글 → OK

[핵심 원칙 요약]
1. AI 언급 → 거의 무조건 HIDE (놓치는 것보다 과하게 잡는 게 낫다)
2. 운영자/제품/영상을 향한 부정 → 강도 상관없이 HIDE (대상이 운영자인지 확신 없어도 가능성 있으면 HIDE)
3. 영상 속 제3자, 다른 댓글러, 업계, 본인을 향한 부정, 단순 비속어 → OK
4. 대상 판단은 반드시 영상 제목/설명과 원댓글을 근거로 할 것

결과를 반드시 아래 JSON 형식으로만 응답하세요. 다른 텍스트 없이 JSON만.
target은 위 1단계의 값 중 하나, reason은 한 줄 요약:
{
  "results": [
    {"id": 1, "target": "영상", "action": "HIDE", "reason": "[AI] 영상이 AI라고 지적"},
    {"id": 2, "target": "운영자", "action": "HIDE", "reason": "운영자 몸 비하"},
    {"id": 3, "target": "영상속인물", "action": "OK", "reason": "영상 주제인 트레이너를 욕함"},
    {"id": 4, "target": "다른댓글러", "action": "OK", "reason": "원댓글 작성자와 논쟁"},
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

def format_comment_for_ai(c: dict) -> str:
    """댓글 1개를 영상 맥락 + 원댓글 맥락과 함께 AI 입력용 블록으로 변환."""
    lines = [f'[{c["id"]}] {c["label"]}']
    lines.append(f'  영상 제목: {c.get("video_title") or "(알 수 없음)"}')
    desc = (c.get("video_description") or "").strip().replace("\n", " ")
    if desc:
        lines.append(f'  영상 설명: {desc[:VIDEO_DESC_MAX_CHARS]}')
    parent = (c.get("parent_text") or "").strip()
    if parent:
        lines.append(f'  원댓글: {parent[:PARENT_TEXT_MAX_CHARS]}')
    lines.append(f'  댓글: {c["text"][:500]}')
    return "\n".join(lines)

def ai_judge_batch(client: anthropic.Anthropic, comments: list[dict]) -> list[dict]:
    comment_list = "\n\n".join(format_comment_for_ai(c) for c in comments)
    user_message = (
        "아래 댓글들을 판단해주세요. "
        "각 댓글의 영상 제목/설명과 원댓글을 근거로 먼저 대상을 정한 뒤 HIDE/OK를 결정하세요.\n\n"
        f"{comment_list}"
    )

    try:
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=3000,
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

def get_video_info(youtube, video_ids: list[str]) -> dict[str, dict]:
    """영상 ID 목록 → {video_id: {"title", "description"}}. 50개씩 일괄 조회."""
    info = {}
    ids = [v for v in dict.fromkeys(video_ids) if v]
    for i in range(0, len(ids), 50):
        chunk = ids[i:i + 50]
        try:
            resp = youtube.videos().list(part="snippet", id=",".join(chunk)).execute()
        except HttpError as e:
            log.warning(f"영상 정보 조회 실패 ({len(chunk)}개): {e}")
            continue
        for item in resp.get("items", []):
            sn = item.get("snippet", {})
            info[item["id"]] = {
                "title": sn.get("title", ""),
                "description": sn.get("description", ""),
            }
    return info

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
        video_id = snippet.get("videoId") or top_snippet.get("videoId", "")
        top_text = top_snippet.get("textOriginal", top_snippet.get("textDisplay", ""))
        top_comment["_is_reply"] = False
        top_comment["_is_pinned_reply"] = False
        top_comment["_video_id"] = video_id
        top_comment["_parent_text"] = ""
        normal_top.append(top_comment)

        # 대댓글이 있으면 가져오기 (원댓글 본문을 함께 붙여서 대상 판단에 활용)
        if reply_count > 0:
            thread_id = top_comment["id"]
            replies = get_replies(youtube, thread_id, is_pinned=thread.get("_is_pinned", False))
            for r in replies:
                r["_video_id"] = r.get("snippet", {}).get("videoId") or video_id
                r["_parent_text"] = top_text
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

        # 대상 판단용 영상 제목/설명 조회
        video_info = get_video_info(youtube, [c.get("_video_id", "") for c in all_comments])
        log.info(f"영상 정보 {len(video_info)}개 조회됨")

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
                vinfo = video_info.get(comment.get("_video_id", ""), {})
                ai_input.append({
                    "id": i,
                    "label": label,
                    "text": text,
                    "video_title": vinfo.get("title", ""),
                    "video_description": vinfo.get("description", ""),
                    "parent_text": comment.get("_parent_text", ""),
                })
                comment_map[i] = comment

            scanned_count += len(batch)

            log.info(f"AI 판단 중... ({batch_start+1}~{batch_start+len(batch)}개)")
            results = ai_judge_batch(ai, ai_input)
            ai_call_count += 1

            for result in results:
                idx = result.get("id")
                action = result.get("action", "OK")
                target = result.get("target", "")
                reason = result.get("reason", "")
                if target:
                    reason = f"[대상:{target}] {reason}"

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
