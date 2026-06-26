#!/usr/bin/env python3
"""
YouTube 악플 자동 숨기기 봇 (AI 판단 버전)
- Claude AI가 프롬프트 기반으로 댓글을 판단
- 댓글을 배치로 묶어서 한 번에 판단 (API 비용 절약)
- 숨기기(heldForReview)만, 삭제 없음
- 반복 악플러 추적 → 대시보드 표시
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

REPEAT_THRESHOLD = 3   # 몇 번 숨겨지면 대시보드에 표시
AI_BATCH_SIZE = 20     # 한 번에 AI에게 보낼 댓글 수 (비용 절약)

# ──────────────────────────────────────────────────────────────────────────────
# ★ 여기를 수정해서 AI 판단 기준을 바꾸세요 ★
# ──────────────────────────────────────────────────────────────────────────────
MODERATION_PROMPT = """
당신은 유튜브 채널 댓글 관리자입니다.
아래 댓글 목록을 보고 각각 숨겨야 할지(HIDE) 괜찮은지(OK) 판단하세요.

[숨겨야 할 댓글 기준]
- 욕설, 비속어, 혐오 표현 (한국어/영어 모두)
- 특정인 비하, 인신공격
- 스팸, 광고, 홍보 링크
- 도배, 의미없는 반복
- 악의적인 조롱, 폄하

[숨기면 안 되는 댓글]
- 비판이더라도 건설적이고 예의 바른 의견
- 질문이나 일반적인 피드백
- 칭찬, 응원 댓글
- 가벼운 농담이나 재미있는 댓글

결과를 반드시 아래 JSON 형식으로만 응답하세요. 다른 텍스트 없이 JSON만:
{
  "results": [
    {"id": 1, "action": "HIDE", "reason": "욕설 포함"},
    {"id": 2, "action": "OK", "reason": "일반 피드백"},
    ...
  ]
}
"""
# ──────────────────────────────────────────────────────────────────────────────

# ─── 로깅 ─────────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler()
    ]
)
log = logging.getLogger(__name__)

# ─── 인증 ─────────────────────────────────────────────────────────────────────

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
                print("Google Cloud Console → OAuth 2.0 클라이언트 ID 생성 후")
                print(f"'{CREDENTIALS_FILE}'로 저장하세요.")
                print("가이드: https://developers.google.com/youtube/v3/quickstart/python\n")
                raise FileNotFoundError(CREDENTIALS_FILE)
            flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_FILE, SCOPES)
            creds = flow.run_local_server(port=0)
        with open(TOKEN_FILE, "w") as f:
            f.write(creds.to_json())
    return build("youtube", "v3", credentials=creds)

def get_anthropic_client():
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("\n❌ ANTHROPIC_API_KEY 환경변수가 없습니다.")
        print("설정 방법:")
        print("  export ANTHROPIC_API_KEY=sk-ant-...")
        print("또는 .env 파일에 ANTHROPIC_API_KEY=sk-ant-... 추가\n")
        raise EnvironmentError("ANTHROPIC_API_KEY not set")
    return anthropic.Anthropic(api_key=api_key)

# ─── 데이터 관리 ──────────────────────────────────────────────────────────────

def load_data():
    if Path(DATA_FILE).exists():
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {
        "offenders": {},
        "processed": [],
        "stats": {
            "total_hidden": 0,
            "total_scanned": 0,
            "ai_calls": 0,
            "last_run": None
        }
    }

def save_data(data):
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

# ─── AI 판단 ──────────────────────────────────────────────────────────────────

def ai_judge_batch(client: anthropic.Anthropic, comments: list[dict]) -> list[dict]:
    """
    댓글 배치를 AI에게 보내서 HIDE/OK 판단받기
    comments: [{"id": 1, "text": "댓글내용"}, ...]
    returns: [{"id": 1, "action": "HIDE", "reason": "..."}, ...]
    """
    comment_list = "\n".join(
        f'[{c["id"]}] {c["text"][:300]}'  # 너무 긴 댓글은 300자로 자름
        for c in comments
    )

    user_message = f"아래 댓글들을 판단해주세요:\n\n{comment_list}"

    try:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",  # 빠르고 저렴한 모델 사용
            max_tokens=1000,
            system=MODERATION_PROMPT,
            messages=[{"role": "user", "content": user_message}]
        )

        raw = response.content[0].text.strip()

        # JSON 파싱 (마크다운 코드블록 제거)
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

# ─── YouTube 연동 ─────────────────────────────────────────────────────────────

def hide_comment(youtube, comment_id: str):
    youtube.comments().setModerationStatus(
        id=comment_id,
        moderationStatus="heldForReview"
    ).execute()

def get_channel_comments(youtube, channel_id: str, max_results: int = 200):
    all_comments = []
    page_token = None
    while True:
        params = {
            "part": "snippet",
            "allThreadsRelatedToChannelId": channel_id,
            "maxResults": min(max_results - len(all_comments), 100),
            "order": "time",
            "moderationStatus": "published",
        }
        if page_token:
            params["pageToken"] = page_token
        response = youtube.commentThreads().list(**params).execute()
        all_comments.extend(response.get("items", []))
        page_token = response.get("nextPageToken")
        if not page_token or len(all_comments) >= max_results:
            break
    return all_comments

def get_my_channel_id(youtube):
    response = youtube.channels().list(part="id,snippet", mine=True).execute()
    items = response.get("items", [])
    if not items:
        raise ValueError("채널을 찾을 수 없습니다.")
    ch = items[0]
    log.info(f"채널: {ch['snippet']['title']} ({ch['id']})")
    return ch["id"]

# ─── 메인 ─────────────────────────────────────────────────────────────────────

def run_moderation(max_comments: int = 200):
    log.info("=" * 50)
    log.info("AI 악플 자동 숨기기 봇 시작")

    youtube = get_authenticated_service()
    ai = get_anthropic_client()
    data = load_data()
    channel_id = get_my_channel_id(youtube)

    hidden_count = 0
    scanned_count = 0
    ai_call_count = 0

    try:
        threads = get_channel_comments(youtube, channel_id, max_comments)
        log.info(f"댓글 {len(threads)}개 가져옴")

        # 이미 처리한 댓글 제외
        new_threads = [
            t for t in threads
            if t["snippet"]["topLevelComment"]["id"] not in data["processed"]
        ]
        log.info(f"새 댓글 {len(new_threads)}개 판단 필요")

        # 배치 단위로 AI 판단
        for batch_start in range(0, len(new_threads), AI_BATCH_SIZE):
            batch = new_threads[batch_start : batch_start + AI_BATCH_SIZE]

            # AI에 보낼 형식으로 변환
            ai_input = []
            thread_map = {}  # AI id(1,2,3...) → thread 매핑
            for i, thread in enumerate(batch, 1):
                top = thread["snippet"]["topLevelComment"]
                text = top["snippet"].get("textOriginal", top["snippet"].get("textDisplay", ""))
                ai_input.append({"id": i, "text": text})
                thread_map[i] = thread

            scanned_count += len(batch)

            log.info(f"AI 판단 중... (댓글 {batch_start+1}~{batch_start+len(batch)}개)")
            results = ai_judge_batch(ai, ai_input)
            ai_call_count += 1

            for result in results:
                idx = result.get("id")
                action = result.get("action", "OK")
                reason = result.get("reason", "")

                if idx not in thread_map:
                    continue

                thread = thread_map[idx]
                top = thread["snippet"]["topLevelComment"]
                comment_id = top["id"]
                snippet = top["snippet"]
                text = snippet.get("textOriginal", snippet.get("textDisplay", ""))
                author_name = snippet.get("authorDisplayName", "알 수 없음")
                author_channel_id = snippet.get("authorChannelId", {}).get("value", "unknown")

                data["processed"].append(comment_id)

                if action == "HIDE":
                    try:
                        hide_comment(youtube, comment_id)
                        hidden_count += 1

                        # 오펜더 기록
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

                        log.info(f"숨김: @{author_name} | {reason} | \"{text[:40]}...\"")
                        time.sleep(0.3)  # YouTube API 쿼터 보호

                    except HttpError as e:
                        log.error(f"숨김 실패 ({comment_id}): {e}")

            # AI API 속도 제한 방지
            if batch_start + AI_BATCH_SIZE < len(new_threads):
                time.sleep(1)

        # 통계 저장
        data["stats"]["total_hidden"] += hidden_count
        data["stats"]["total_scanned"] += scanned_count
        data["stats"]["ai_calls"] = data["stats"].get("ai_calls", 0) + ai_call_count
        data["stats"]["last_run"] = datetime.now().isoformat()

        if len(data["processed"]) > 5000:
            data["processed"] = data["processed"][-5000:]

        save_data(data)

        # 결과 출력
        repeat_offenders = {
            cid: info for cid, info in data["offenders"].items()
            if info["count"] >= REPEAT_THRESHOLD
        }

        log.info(f"\n{'='*50}")
        log.info(f"결과: {scanned_count}개 스캔 → {hidden_count}개 숨김 (AI 호출 {ai_call_count}회)")
        log.info(f"누적: 총 {data['stats']['total_hidden']}개 숨김, AI {data['stats']['ai_calls']}회 호출")

        if repeat_offenders:
            log.info(f"\n⚠️  반복 악플러 {len(repeat_offenders)}명 (직접 영구 숨기기 필요):")
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
    args = parser.parse_args()

    if args.watch:
        log.info("지속 실행 모드: 10분마다 실행")
        while True:
            try:
                run_moderation(args.max)
            except Exception as e:
                log.error(f"실행 오류: {e}")
            log.info("10분 후 재실행...")
            time.sleep(600)
    else:
        run_moderation(args.max)
