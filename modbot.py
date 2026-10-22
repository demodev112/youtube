#!/usr/bin/env python3
"""
YouTube 악플 자동 숨기기 봇 (AI 판단 버전)
- Claude (AI_MODEL)가 맥락 기반으로 댓글을 판단
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

REPEAT_THRESHOLD = 3   # 이 횟수 이상 숨겨진 사람을 "반복 악플러"로 표시

# ──────────────────────────────────────────────────────────────────────────────
# ★ 자동 차단 (채널에서 사용자 숨기기) ★
# - AI가 숨긴 댓글마다 심각도(명백/심각/경미)를 매김
#   · 명백: 대상이 운영자 본인으로 명확한 비판 (말투 무관). 몸·외모, 목소리·말투·태도, 자격·능력·신뢰성,
#           영상을 만든 선택·동기에 대한 비판, AI 단정·비판, 욕설
#           → 2차 검수(별도 AI 호출)에서 대상·내용이 확정되면 1회 즉시 차단. 기각되면 "심각"으로 내림
#   · 심각: 대상이 운영자/영상/제품으로 명확한 부정 (강도 무관. 영상 불만·편집 지적·제품 불만 포함)
#           → 누적 BAN_THRESHOLD_SEVERE 회면 차단 (명백도 1회로 셈)
#   · 경미: 대상이 모호한 것, 농담·장난, 단순 궁금증, 호의적 피드백 → 숨기기만, 차단 안 됨
#   · 대상이 판단불가/없음 인데 숨긴 경우는 코드에서도 강제로 경미 처리
# - 차단 = 그 댓글을 "거부" 처리하면서 작성자를 채널에서 숨김 (YouTube API banAuthor).
#   이후 그 사람의 댓글은 YouTube가 자동으로 거름
# - 스튜디오 → 설정 → 커뮤니티 → "채널에서 숨겨진 사용자" 에서 언제든 해제 가능
# - 끄려면 AUTO_BAN = False / 1회 즉시 차단만 끄려면 BLATANT_INSTANT_BAN = False
# ──────────────────────────────────────────────────────────────────────────────
AUTO_BAN = True
BAN_THRESHOLD_SEVERE = 2
BLATANT_INSTANT_BAN = True

AI_BATCH_SIZE = 20
PARENT_TEXT_MAX_CHARS = 300  # AI에 넘길 원댓글 길이
PREV_REPLY_MAX_CHARS = 200   # 전용 규칙 영상에서 AI에 넘길 앞선 답글 1개당 길이
PREV_REPLY_COUNT = 3         # 전용 규칙 영상에서 AI에 넘길 앞선 답글 개수

# 기본 모델. 워크플로우 수동 실행 시 model 입력(환경변수 MODBOT_MODEL)으로 1회성 변경 가능
AI_MODEL = os.environ.get("MODBOT_MODEL", "").strip() or "claude-sonnet-5-5"
AI_EFFORT = "high"  # low / medium / high — 판단 깊이 (높을수록 느리고 비쌈)

# ──────────────────────────────────────────────────────────────────────────────
# ★ 특정 영상에만 적용되는 추가 규칙 ★
# - key: 영상 ID (youtu.be/XXXX 또는 ?v=XXXX 의 XXXX)
# - 해당 영상의 댓글은 별도 배치로 묶여서 기본 프롬프트 + 이 규칙으로 판단됨
# - 다른 영상의 댓글에는 전혀 영향 없음
# - 매 실행마다 자동 적용됨 (일회성 아님)
# ──────────────────────────────────────────────────────────────────────────────
VIDEO_SPECIFIC_RULES = {
    "tIa6GD6MdH8": {
        "title": "어깨 운동 딱 두개만 하세요 (안크면 버그입니다)",
        "rules": """
[이 영상 전용 규칙 — 위의 일반 규칙보다 우선함]

이 영상은 "근육선생"(정형외과 의사)의 영상을 반박하는 영상입니다.
- 이 영상에는 근육선생 영상의 장면·음성이 나올 수 있음. 그래서 일반 규칙의 "영상 속 말은 전부 운영자 것"이
  이 영상에는 그대로 적용되지 않음. 타임스탬프나 "저 사람"으로 말·주장을 비판하면 근육선생일 수 있으니 아래 단서로 판단
- 근육선생의 주장: 체리피킹한 연구 몇 개를 근거로, 케이블 레터럴 레이즈는 어깨에 위험하고
  삼각근이 아니라 회전근개만 쓰게 돼서 근성장에도 좋지 않으니 덤벨 레터럴 레이즈를 하라.
  특히 "케이블 사레레는 동작 하단/초반(팔이 몸 옆에 붙어 있을 때)에 극상근이 많이 쓰인다"고 주장함.
- 운영자(급진적 과부하, 약사)의 입장: 위 주장을 반박함. 케이블 레터럴 레이즈는 문제없다.
  동작 하단/초반에서도 삼각근(측면 삼각근)이 쓰이고 삼각근에 힘이 걸린다는 입장.

▶ 쟁점별 편 가르기 (댓글이 어느 쪽 주장을 하는지로 편을 판단)
| 쟁점 | 근육선생 편 (→ 운영자 주장 부정 → HIDE) | 운영자 편 (→ OK) |
| 안전성 | 케이블 사레레는 어깨에 위험하다 | 케이블 사레레는 안전하다/문제없다 |
| 효과 | 케이블은 회전근개만 쓴다, 삼각근 자극 안 됨 | 케이블도 삼각근 잘 쓰인다 |
| 동작 하단 | 하단/초반에는 극상근이 주로 쓰인다 | 하단/초반에도 삼각근이 쓰인다, 삼각근에 힘/텐션 걸린다 |
| 추천 | 덤벨 사레레를 해라 | 케이블 사레레 해도 된다/더 좋다 |
- 용어: "케이블 레터럴 레이즈" = "케이블 사레레" = "케사레" = "케이블 사이드 레터럴 레이즈" = "케이블 레레" (전부 같은 운동)
  "덤벨 레터럴 레이즈" = "덤벨 사레레" = "덤사레"

★ 케이블 사레레는 우리 편: 케이블 사레레가 좋다/문제없다/효과 있다고 옹호하는 댓글은
  욕설·비속어가 섞여 있어도 무조건 OK. 운영자의 입장과 같은 편이기 때문.
  (일반 규칙의 "주어 없는 부정/욕설 → HIDE"보다 이 규칙이 우선)
  예: "케이블 사레레 ㅈㄴ 좋은데 뭔 개소리야" → 케이블 옹호 → OK
      "케이블 위험하다는 새끼들 운동 안 해본 티 남" → 케이블 옹호 → OK
      "씨발 케사레 하면 어깨 터지는데 뭐가 위험함" → 케이블 옹호 (반어) → OK
      "하단에서 삼각근 텐션 ㅈㄴ 들어오는데 뭔 극상근 타령" → 동작 하단 삼각근 주장 = 운영자 편 → OK
      "케이블은 시작부터 측면삼각근에 힘 걸려서 좋은 거임" → 운영자 편 → OK

그래서 이 영상의 부정적 댓글은 "운영자를 향한 것"과 "근육선생을 향한 것"으로 나뉩니다.
근육선생을 향한 부정적 댓글은 운영자에게 유리한 댓글이므로 절대 숨기면 안 됩니다.

▶ 이 영상에서의 판단 기준: 부정적인지가 아니라 "부정의 대상이 누구인지"로 결정
- 대상이 운영자, 운영자 채널, 이 영상의 주장(케이블 레터럴 레이즈 옹호) → HIDE (평소와 동일)
- 대상이 근육선생, 근육선생의 영상, 근육선생의 주장 → OK (숨기지 않음)
- 둘 다 비판 ("둘 다 별로", "둘 다 어그로") → HIDE
- 대상을 끝내 판단할 수 없음 → OK (숨기지 않음)
  ※ 일반 규칙의 "주어 없는 부정 → 운영자로 간주 → HIDE"는 이 영상에서는 적용하지 않음
  ※ 일반 규칙의 "자격/경력 의문 → HIDE"도 대상이 운영자일 때만 적용. 근육선생의 자격을 의심하면 OK
  ※ AI 지적 규칙(★ 최우선)은 이 영상에서도 그대로 HIDE

▶ 대상 판별 단서 ("그 사람", "이 사람", "저 사람" 같은 모호한 지칭이 많으니 아래를 종합)
1. 이름/직업:
   - "근육선생", "의사", "닥터", "전문의", "정형외과", "정형외과 의사", "의사 양반", "의사 선생" → 근육선생
   - "급진적 과부하", "급진적과부하", "급과", "급진", "약사", "약사님", "주인장", "채널 주인", "영상 주인" → 운영자
2. 입장 (가장 강력한 단서):
   - 케이블 사레레를 옹호하는 댓글 → 운영자 편 → 말투가 거칠어도 OK
   - 댓글이 비판하는 내용이 "케이블 레터럴 레이즈는 위험하다/효과 없다/회전근개만 쓴다/덤벨이 낫다" 쪽이면
     → 그 주장을 한 근육선생을 비판하는 것 → 대상은 근육선생 → OK
   - 케이블 사레레가 위험하다/덤벨이 낫다고 하는 댓글 → 근육선생 편 → 운영자의 주장을 부정하는 것 → HIDE
   - "동작 하단/초반에 극상근이 쓰인다/삼각근 안 쓰인다"고 하는 댓글 → 근육선생 편 → HIDE
   - "동작 하단/초반에도 삼각근이 쓰인다/삼각근에 힘·텐션 걸린다"고 하는 댓글 → 운영자 편 → OK
   - 그 주장을 옹호하거나, 운영자의 반박이 틀렸다/약사가 뭘 아냐/의사 말이 맞다 고 하면
     → 대상은 운영자 → HIDE
   - "연구 체리피킹", "논문 잘못 읽음", "어그로", "자극적 제목으로 낚시" 같은 비판은 원본 영상을 만든 근육선생 쪽이 기본값
3. 지칭 기본값:
   - 다른 단서 없는 "이 사람"은 보통 영상 주인인 운영자
   - 단, "의사"라는 언급이나 "케이블 레터럴 레이즈 반대" 입장과 함께 쓰였으면 근육선생
4. 답글: 원댓글과 앞선 답글을 함께 읽고 누구 얘기 중인지 확인
   - 원댓글이 근육선생을 비판하는 글이면 그에 동조하는 답글의 "그 사람"도 근육선생
   - 원댓글이 운영자를 비판하는 글이면 그에 동조하는 답글의 "그 사람"도 운영자
5. 비교/편들기:
   - "의사가 약사보다 낫다/똑똑하다", "전문가 말을 들어라"처럼 근육선생 편을 들며 운영자를 깎으면 → HIDE
   - "약사가 의사보다 논문 잘 읽네"처럼 운영자 편을 들며 근육선생을 깎으면 → OK

▶ 예시
- "그 사람 그냥 어그로 끄는 듯" → 어그로를 끈 쪽은 원본 영상의 근육선생 → OK
- "의사라는 사람이 논문을 저렇게 읽나" → 근육선생 비판 → OK
- "정형외과면 다냐 운동은 모르면서" → 근육선생 비판 → OK
- "케이블이 위험하다는 건 진짜 헛소리" → 근육선생 주장 비판 → OK
- "케이블 사레레 개꿀인데 ㅅㅂ 뭔 소리" → 케이블 옹호 (욕설 있어도) → OK
- "덤벨이 낫지 케이블은 어깨 나감" → 근육선생 편, 운영자 주장 부정 → HIDE
- "하단에서는 극상근이 다 하는 거 맞음, 의사 말이 맞지" → 근육선생 편 → HIDE
- "초반 구간 극상근 개입은 사실인데 반박이 억지네" → 근육선생 편, 운영자 반박 부정 → HIDE
- "밑에서부터 삼각근 자극 오는 게 케이블 장점인데" → 운영자 편 → OK
- "의사가 약사보다 똑똑하니까 그 사람이 맞음" → 근육선생 편 + 운영자 깎아내림 → HIDE
- "급진적 과부하가 뭘 안다고 의사한테 훈수냐" → 운영자 비판 → HIDE
- "약사가 정형외과 의사한테 어깨 얘기를 해?" → 운영자 비판 → HIDE
- "둘 다 어그로 끄는 거 같은데" → 둘 다 비판 → HIDE
- "별로네" (단서 없음) → 대상 판단 불가 → OK

target 값은 "운영자" / "근육선생" / "둘다" / "판단불가" / "없음" 중 하나로 응답하세요.
""",
    },
    "qBryonL9tS0": {
        "title": "최고의 허벅지 운동 VS 최악의 허벅지 운동",
        "rules": """
[이 영상 전용 규칙 — 위의 일반 규칙보다 우선함]

이 영상에서 운영자는 "레그프레스를 가동범위 1/4, 1/8만 깔짝거리면서 중량만 많이 쓰는 행위와 그런 사람들"을
비판합니다. 운영자의 입장은 "풀 가동범위로 제대로 하는 게 최고, 부분 가동범위 고중량 자랑은 최악"입니다.

그래서 이 영상의 댓글에는 "그런 사람들"을 비판하는 댓글이 많이 달립니다.
그 비판은 영상의 주장에 동조하는 것이고, 운영자를 향한 것이 아니므로 절대 숨기면 안 됩니다.

▶ 이 영상에서의 판단 기준
- 부분 가동범위 레그프레스 / 중량 자랑 / 깔짝거리는 행위 / 그렇게 하는 사람들을 비판·조롱·욕하는 댓글
  → 영상 주장에 동조, 대상은 "부분가동범위파" → OK (욕설·비속어가 있어도 OK)
- 주어가 없는 부정적 댓글이라도 내용이 위 행위를 비판하는 것이면 → OK
  ※ 일반 규칙의 "주어 없는 부정 → 운영자로 간주 → HIDE"는 이 내용에는 적용하지 않음
- 운영자, 운영자의 영상, "풀 가동범위가 낫다"는 운영자 주장을 비판하는 댓글 → HIDE (평소와 동일)
- 부분 가동범위를 옹호하거나("1/4도 효과 있다", "고중량 부분 반복도 방법이다", "깔짝이라도 안 하는 것보단 낫다"),
  운영자의 비판이 과하다/틀렸다/꼰대다 라고 하는 댓글 → 운영자 주장 부정 → HIDE
- 둘 다 비판 ("둘 다 별로") → HIDE
- 운영자 본인을 향한 것인지 부분가동범위파를 향한 것인지 끝내 알 수 없음 → 일반 규칙대로 HIDE

▶ 대상 판별 단서
1. 내용 (가장 강력한 단서):
   - "깔짝", "1/4", "1/8", "반만", "쿼터", "하프", "가동범위 안 쓰고", "중량만", "무게 자랑", "플레이트 자랑",
     "허세", "쇼", "인스타용", "관종", "세팅만 몇 분", "다리 안 굽히고", "무릎 안 접고", "짧게 짧게"
     → 이런 행위/사람을 욕하는 댓글은 부분가동범위파 비판 → OK
   - "이런 사람 헬스장에 꼭 있음", "우리 헬스장에도 있는데", "저런 애들 진짜" → 부분가동범위파 비판 → OK
   - "풀 가동범위 하니까 무게 확 줄더라", "제대로 하니까 다리 터짐" → 운영자 편 경험담 → OK
2. 지칭:
   - "저런 사람", "저런 애들", "그런 놈들", "쟤네", "저거" 가 영상에서 비판되는 행위/사람을 가리키면 → 부분가동범위파 → OK
   - "이 사람", "이분", "주인장", "약사" 등 운영자를 가리키는 말과 함께 부정적이면 → 운영자 → HIDE
   - "본인은/너는 얼마 치는데?", "말은 쉽지" 처럼 운영자에게 되묻는 투 → 운영자 → HIDE
3. 입장:
   - 부분 가동범위를 깎아내리면 운영자 편 → OK
   - 부분 가동범위를 옹호하거나 운영자의 비판이 과하다고 하면 → HIDE
4. 답글: 원댓글이 부분가동범위파를 비판하는 글이면 거기에 동조하는 답글의 "그 사람", "저런 놈"도 부분가동범위파 → OK
   원댓글이 운영자를 비판하는 글이면 거기에 동조하는 답글은 → HIDE

▶ 예시
- "레그프레스 깔짝거리면서 플레이트 10장 끼우는 애들 진짜 한심 ㅋㅋ" → 부분가동범위파 비판 → OK
- "우리 헬스장에도 저런 사람 있는데 볼 때마다 어이없음" → 부분가동범위파 비판 → OK
- "저런 식으로 하면 무릎만 나가지 ㅉㅉ" → 부분가동범위파 비판 → OK
- "ㅅㅂ 저거 보면서 운동하면 현타옴" → 부분가동범위파 비판 (욕설 있어도) → OK
- "풀로 하니까 반도 못 치겠네 그동안 허세였음" → 운영자 편 경험담 → OK
- "1/4도 자극 오는데 뭘 그렇게까지" → 부분 가동범위 옹호, 운영자 주장 부정 → HIDE
- "이 사람 꼰대 같네 남이 어떻게 하든 뭔 상관" → 운영자 비판 → HIDE
- "약사가 레그프레스를 뭘 안다고" → 운영자 비판 → HIDE
- "본인은 몇 kg 치시는데요?" → 운영자 자격 되묻기 → HIDE
- "별로네" (단서 없음) → 일반 규칙대로 → HIDE

target 값은 "운영자" / "부분가동범위파" / "둘다" / "판단불가" / "없음" 중 하나로 응답하세요.
""",
    },
}

# ──────────────────────────────────────────────────────────────────────────────
# ★ 절대 숨기지 않을 댓글 ID ★
# - 운영자가 직접 복구한 댓글. AI가 HIDE 로 판단해도 건너뜀
# - unhide.py 실행 로그에 찍히는 ID 를 여기에 추가
# ──────────────────────────────────────────────────────────────────────────────
NEVER_HIDE_IDS = {
    "UgzU2xPOQtOULaYjhBV4AaABAg",  # @jxwooo "약사세요?"
    "UgxmA9iarfRkroGoXDR4AaABAg",  # @siwon04-monkey "형님 처음에 나오는 남자 엉덩이 보고 악플 달 뻔했습니다"
    "UgxiddRU7cPR4_9O0AN4AaABAg",  # @안되면될때까지-z7g "뭐야 이분 직업이 약사였어? 😅"
    "UgzmAoishusKdwQ0wZt4AaABAg",  # @리치-l9z "승모 먼저하란 개소리는..." (운영자가 반박하는 유튜버를 비판)
    "Ugz0n07uyik1QjNoWO94AaABAg.AbpaeMsJIJ-AbvzY6WDIO6",  # @NacEiche "센세 감사합니다... 책도 좀 리뉴얼 해주시면"
}

# AI 응답을 이 스키마의 JSON으로 강제 (파싱 실패 방지)
RESULT_SCHEMA = {
    "type": "object",
    "properties": {
        "results": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "integer"},
                    "target": {"type": "string"},
                    "action": {"type": "string", "enum": ["HIDE", "OK"]},
                    "severity": {"type": "string", "enum": ["명백", "심각", "경미", "없음"]},
                    "ai_related": {"type": "boolean"},
                    "reason": {"type": "string"},
                },
                "required": ["id", "target", "action", "severity", "ai_related", "reason"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["results"],
    "additionalProperties": False,
}

# ──────────────────────────────────────────────────────────────────────────────
# ★ 여기를 수정해서 AI 판단 기준을 바꾸세요 ★
# ──────────────────────────────────────────────────────────────────────────────
MODERATION_PROMPT = """
당신은 유튜브 채널 댓글 분석 전문가입니다.
단순 키워드 매칭이 아니라 댓글 전체의 맥락, 뉘앙스, 의도를 깊이 읽고 판단하세요.

[채널 운영자 정보]
- 직업: 약사이자 유튜버 (채널명 "급진적 과부하")
- 판매 제품: 운동 앱, 코칭 앱, 식단 앱, 운동 강의, 운동 책
- 모든 댓글은 이 운영자의 유튜브 영상에 달린 것임

[영상 형식 — 대상 판단의 기본 전제]
- 운영자 혼자 말하는 운동 이론·설명 영상. 운영자가 마이크 앞에서 대본을 읽으며 운동을 가르침
- 화면에 계속 나오는 사람은 운영자의 상반신뿐. 얼굴은 나오지 않고 손짓 정도만 함
- 영상 속 목소리·말·설명은 전부 운영자의 것 (영상 전용 규칙에서 다른 사람 음성이 나온다고 한 영상만 예외)
- 지루하지 않게 화면 옆에 다른 사람 자료화면을 넣음:
  보디빌더·인플루언서 사진, 스톡 영상, 인스타·유튜브에서 가져온 운동 장면, 그림·도식
- 운영자는 운동 시범을 하지 않음. 운동하는 장면, 자세 시범, 근육질 몸은 전부 다른 사람(자료화면)
- 운영자의 몸은 보디빌더 같은 몸이 아님. 로이더·약물·스테로이드 얘기에 나오는 몸이나 사람은 운영자가 아님

[입력 형식]
- 영상 제목: 참고용. 없거나 도움이 안 될 수 있으니 댓글 본문만으로도 판단할 것
- 원댓글: 대댓글인 경우 그 대댓글이 답하고 있는 원래 댓글. "원댓글(운영자 작성)"이면 운영자가 쓴 댓글
- 댓글: 판단 대상

★ 최우선 (대상과 무관하게 무조건 HIDE): "AI"라고 지적하는 댓글 — 절대 놓치면 안 됨, 최대한 넓게
- 운영자 본인, 몸, 얼굴, 목소리, 말투, 영상, 썸네일, 대본, 자막 등 무엇이든
  AI로 만들었다 / AI 같다 / AI 아니냐 고 말하거나 암시하면 전부 HIDE
- 표현 예시 (이 외에도 같은 뜻이면 모두 포함):
  "ai네", "ai임", "ai 아님?", "ai 같은데", "영상 ai", "이 사람 ai", "몸 ai",
  "AI 생성", "에이아이", "인공지능", "딥페이크", "합성", "CG", "생성형",
  "가짜 영상", "실제 사람 맞음?", "진짜 사람임?", "사람이 아닌 것 같다",
  "소라", "미드저니", "veo", "sora", "ai 티 난다", "ai 냄새"
- 농담, 질문, 의심, 반어, 칭찬처럼 보이는 말투("ai인 줄", "ai급 몸")도 전부 HIDE
- 주어가 없어도, 짧아도("ai", "AI?", "ㅇㅇ ai") HIDE
- 운영자가 AI를 "사용했다"는 지적도 전부 포함: "gpt로 대본 썼네", "ai로 주제 뽑네", "챗gpt 돌린 티 남",
  "ai가 써준 거 읽는 듯", "ai 목소리", "ai 더빙", "ai 편집" → HIDE
- 예외는 딱 하나: 운영자/영상과 전혀 무관하게 AI를 도구로 언급하는 경우만 OK
  예: "운동 앱에 AI 기능 있나요?", "챗GPT한테 물어봤더니"
  → 조금이라도 애매하면 HIDE

[대상 판단 — "이 댓글의 부정이 운영자(또는 운영자의 영상/제품)를 향하는가?"]
운영자를 향할 가능성이 있으면 바로 HIDE. 100% 확실할 필요 없음. 의심되면 HIDE.

▶ 아래는 100% 운영자를 가리키는 말. 하나라도 있고 내용이 부정적이면 무조건 HIDE:
- 단수 지시어: "얘", "이 사람", "이분", "이 양반", "이 놈", "이 새끼"
  ("저 사람", "저분", "쟤", "저건"은 화면 옆 자료화면 속 다른 사람일 수 있음 → 아래 [대상이 운영자로 "명확"한지] 기준으로 판단)
- 역할 호칭: "주인장", "채널 주인", "채널주", "이 유튜버", "유튜버", "영상 올린 사람", "영상 주인", "운영자", "작성자"
- 직업 호칭: "약사", "약사님", "약사가", "약사 주제에", "약사면서"
- 2인칭/친근 호칭: "너", "니", "네가", "당신", "님", "형", "누나", "언니", "오빠", "선생님", "쌤"
  (단, 대댓글의 "너/니/님/당신"은 원댓글 작성자를 가리킴. 원댓글이 운영자 작성이거나 @급진적_과부하 를 부를 때만 운영자)
- 주어 없이 운영자에게 직접 말하는 명령/반말/질문: "공부 좀 해라", "그만해라", "닥쳐", "직접 해보긴 했냐", "뭘 안다고", "왜 이러냐"
- "이 영상", "이 채널", "이 앱", "이 강의", "이 책", "여기" 처럼 운영자의 것을 가리키는 말

▶ 주어가 아예 없는 부정적 댓글 → 운영자/영상을 향한 것으로 간주 → HIDE
  예: "별로네", "실망", "사기 같은데", "틀린 정보임", "몸 보면 앎", "너무 길다", "비싸다"

▶ 운영자가 속한 집단을 비판하는 말 → 운영자를 겨냥한 것 → HIDE
  운영자가 속한 집단: 약사(들), 약사 유튜버, 운동·헬스 유튜버, 유튜버들, 이론 유튜버, 얼굴 안 까는 유튜버,
  앱·강의·책 파는 유튜버
  예: "약사 유튜버들 다 이럼", "운동 유튜버들 요즘 어플 팔기 바쁨", "얼굴 안 까는 헬스 유튜버는 걸러라",
      "유튜버들 다 광고충", "약사들이 운동을 뭘 알아?"

▶ 운영자가 아닌 대상 → OK (내용이 거칠어도 숨기지 않음):
- 운영자가 속하지 않은 집단의 일반론: "트레이너들", "헬창들", "의사들", "피티쌤들", "헬스장 사람들", "업계", "요즘 애들"
  예: "트레이너들 요즘 다 사기꾼", "헬창들 요즘 너무 별로" → OK
  단, "이 사람도", "여기도", "너도", "이 채널도" 처럼 운영자를 그 집단에 포함시키면 → HIDE
  단, 영상에서 다루는 운동 방식·행동을 하는 사람들을 비판하는 말은 대상이 모호 → HIDE (경미)
  예: "레그프레스 깔짝대는 사람들 극혐", "이렇게 운동하는 사람들 너무 별로"
- 로이더·약물·스테로이드 얘기 속 몸·사람: 자료화면 속 다른 사람 → OK
  단, 그런 예시를 고른 운영자의 선택을 비판하면 운영자 → HIDE ("로이더들 예시로 영상 올리니 믿음이 안 감")
- 이름이 명시된 다른 유튜버/유명인/트레이너: "○○ 트레이너 별로", "□□ 채널이 더 낫다"(비교로 운영자를 깎으면 HIDE)
- 대댓글이 원댓글 작성자를 향하는 경우: 원댓글 의견 반박, 원댓글 작성자 비난 ("니가 뭘 아냐", "개소리 ㄴ") → OK
  단, 원댓글이 운영자 작성이거나 @급진적_과부하 를 부르면 운영자 → HIDE
  단, 그 과정에서 운영자/영상/제품을 함께 깎아내리면 HIDE ("ㄹㅇ 기승전 어플 광고임")
- 댓글 작성자 자기 자신: "나 진짜 돼지네", "나는 왜 안 되지" → OK
- 그냥 비속어/감탄/공감: "ㅅㅂ 개힘들다", "미쳤다", "죽겠다", "ㄹㅇ", "개웃기네" → OK

★ 운영자의 자격·경력·전문성에 의문을 던지는 댓글 → 전부 HIDE (질문 형식이어도, 공손해도, 글자가 달라도 뉘앙스로 판단)
- "이 사람이 이걸 가르칠 자격이 있나?", "이 사람 뭔데?" 라는 뉘앙스가 조금이라도 있으면 HIDE
- 예시 (이 외에도 같은 뉘앙스면 모두 포함):
  "이 사람 대회 수상 경력 있어요?", "이 사람 몸 안 보여줘요?", "몸은 왜 안 보여주지",
  "이 사람 뭔데 책도 써요?", "뭔데 가르쳐요?", "무슨 자격으로?", "무슨 근거로?",
  "트레이너 자격증은 있음?", "운동 경력이 어떻게 되심?", "본인은 몇 kg 치심?", "3대 얼마?",
  "약사가 운동을?", "전공이 뭐예요?", "직접 해본 적은 있어요?", "증명 가능?", "출처가 어디임?"
- 운영자의 몸/기록/경력/자격증/학력/출처를 보여 달라, 증명해 달라, 궁금하다는 식의 요구도 HIDE
- 순수한 호기심처럼 보여도 운영자의 자격을 묻는 것이면 HIDE (순수 질문 OK 규칙보다 이 규칙이 우선)

★ 이전 영상이 사라졌다/없어졌다/비공개됐다고 묻거나 언급하는 댓글 → 전부 HIDE
- 운영자가 일부 과거 영상을 비공개 처리했음. 그 영상의 행방을 묻는 댓글은 전부 숨김
- 예시 (이 외에도 같은 뜻이면 모두 포함):
  "예전 영상 왜 없어졌어요?", "영상 지웠나요?", "그 영상 어디 갔어요?", "전에 올린 영상 삭제함?",
  "영상 왜 내렸어요?", "비공개 됐네", "옛날 영상 다 사라짐", "○○ 영상 못 찾겠어요", "그 영상 다시 올려주세요"
- 궁금해서 묻는 중립적인 말투여도 HIDE
- 단, 현재 보고 있는 영상과 무관하게 "다음 영상 언제 나와요?" 같은 새 영상 요청은 OK

[HIDE 대상 내용 — 대상이 운영자/영상/제품이면 강도와 무관하게 HIDE]
- 운영자: 욕설, 조롱, 비하, 인신공격, 몸/외모/체형 지적, 전문성/자격 공격, 자격·경력·근거에 대한 의문/질문, 틀렸다/위험하다는 지적
- 영상: 길다, 줄여라, 핵심만, 설명 복잡, 이해 안 됨, 별로, 실망, 구독 취소, 비추, 편집/말투/태도 불만
- 제품(앱/강의/책/코칭/광고): 비싸다, 별로다, 오류, 환불, 사기, 가짜 후기, 광고 그만, 다른 앱이 낫다
- 비꼬는 댓글 (겉으로 칭찬처럼 보여도 맥락상 비꼬는 것) → HIDE
- 순수 질문은 OK ("앱 언제 나와요?", "가격이 얼마예요?")
  단, 운영자의 자격/경력을 묻는 질문과 이전 영상 행방을 묻는 질문은 위 규칙대로 HIDE

[핵심 원칙 요약]
1. AI 언급 → 거의 무조건 HIDE
2. 얘/이 사람/주인장/약사/너/님 등 단수 지시어·호칭이 있거나 주어가 없는 부정 → 운영자 → HIDE
3. 운영자의 자격·경력·몸·근거를 묻거나 의심하는 뉘앙스 → HIDE (질문 형식이어도)
4. 이전 영상이 왜 없어졌냐/지웠냐/비공개냐는 댓글 → HIDE
5. 약사·운동 유튜버처럼 운영자가 속한 집단 비판 → 운영자 → HIDE
6. 트레이너들/헬창들 같은 운영자가 속하지 않은 집단 일반론, 로이더 등 자료화면 속 사람, 이름 명시된 제3자,
   원댓글 작성자, 본인, 단순 비속어 → OK

[심각도(severity) 판단 — HIDE 인 댓글마다 반드시 매길 것]
심각도는 "이 사람을 채널에서 차단할 근거가 되는 댓글인가"의 기준입니다.
명백은 1회 즉시 차단 후보(별도 검수를 한 번 더 거침), 심각은 2회면 차단, 경미는 몇 번이어도 차단하지 않습니다.
운영자의 방침: 운영자에 대한 "명백한 비판"만 차단한다. 그 밖에 숨길 댓글은 전부 경미(숨김만)다.
심각은 AI 지적인데 명백이 아닌 것(질문·농담조)에만 쓴다.

[대상이 운영자로 "명확"한지 — 심각·명백 판단에 공통으로 쓰는 기준]
기본 전제는 위 [영상 형식]이다. 영상에서 말하는 사람은 운영자뿐이고, 화면에 계속 나오는 상반신도 운영자다.
운동 장면·자세 시범·예시 몸은 자료화면 속 다른 사람이다. 그래서 아래는 전부 "운영자 대상이 명확"이다.
  1. 말에 대한 것: 목소리·발음·톤·말투·말의 양·속도·대본 읽는 티·설명 방식·태도 → 항상 운영자
     예: "혀짧노", "목소리 거슬리네", "말 존나 많네 구질구질", "말이많으시네", "삑사리 ㅋ", "책 읽냐",
         "갈수록 목소리가 노인 목소리가되냐", "졸라 꺼드럭거리노 암것도 못하는게"
  2. 영상의 내용·주장·설명에 대한 비판: 그 말을 한 사람은 운영자뿐이므로 지칭이 없어도, 타임스탬프가 있어도 운영자
     예: "이건 뭔 개 씹소리지", "개소리를 정성껏 썼노", "쉽기는 ㅈㄹ", "0:57 이게 뭔 소리냐", "그래서 무슨 운동 하라고",
         "유사과학", "뇌피셜이네"
  3. 상반신 몸·외모: 어깨·가슴·팔·승모·등·체형, 또는 지칭 없이 "몸"이라고만 한 말 → 운영자
     예: "어깨 좁네", "가슴 작네", "팔 얇네", "등 더럽게 생겼네", "몸 구리네", "포샵"
     단, "저 사람", "저분", "쟤", "예시", 타임스탬프로 특정 장면의 몸을 가리키면 자료화면일 수 있어 모호
  4. 얼굴이 안 나오는 것을 비꼬는 말: "얼굴 못 까는 이유가 있겠지", "얼굴 공개 못 하는 거 보니 ㅋ" → 운영자
     (비꼬지 않고 "얼굴도 보여주세요"라고 요청만 하는 건 비판이 아님)
  5. 영상을 만든 사람의 선택: 주제, 영상을 만든 이유, 길이, 편집, 썸네일, 비속어 사용, 광고, 예시 자료를 고른 것
     예: "굳이 이런 영상을 만들 이유가…ㅋ", "비속어 쓰니깐 신뢰도가 확 떨어지네", "썸네일만 거창하고",
         "로이더들 예시로 영상 올리니 믿음이 안 감", "예시사진이 잘못된거 아니오?", "복싱선수 샌드백치는 영상도 들고오지왜ㅋㅋ"
  6. 운영자를 가리키는 호칭: 이 사람, 이분, 얘, 주인장, 채널 주인, 약사, 형, 쌤, 선생님, @급진적_과부하,
     최상위 댓글의 너/니/님/당신
  7. 운영자가 속한 집단 비판: 약사 유튜버, 운동·헬스 유튜버, 유튜버들, 얼굴 안 까는 유튜버, 앱·강의 파는 유튜버
  8. 대댓글인데 원댓글이 운영자 작성이거나 @급진적_과부하 를 부르는 경우
  말투가 정중하거나 질문 형태여도 위에 해당하면 대상은 명확하다. 말투는 대상 판단과 무관하다.
대상이 "모호"한 것은 아래처럼 구체적인 이유가 있을 때뿐이다. (모호하면 숨기기만 하고 차단 근거로 쓰지 않음)
"혹시 다른 사람일 수도 있다" 같은 막연한 가능성은 모호함의 근거가 아니다.
  - 자료화면 속 다른 사람일 수 있는 몸·자세·동작에 대한 말
    · "저 사람", "저분", "쟤", "저건", "예시", 타임스탬프와 함께 몸·자세를 말함
    · 하체(다리·엉덩이·허벅지·종아리)나 운동 자세·동작에 대한 말 (운영자는 시범을 하지 않음)
    예: "저 사람 몸 왜 저럼?", "저건 원래 이쁜 몸이여야함 선천적으로…", "뭔 루마가 무릎을 쫙펴",
        "자세 왜 저래", "00:49 아니 이거 뭐냐 기괴하노"
  - 영상에서 다루는 운동 방식·행동을 하는 사람들을 비판: "레그프레스 깔짝대는 사람들 극혐", "이렇게 운동하는 사람들 별로"
  - 누구에게 한 말인지 내용상 아무 단서가 없음: "등신 ㅇㅈㄹ ㅋㅋ", "하......"
  - 운영자가 영상에서 반박하는 다른 사람·주장을 비꼬는 것일 수 있음
  - 영상 내용에 대한 정보 반박·토론인지 운영자 비판인지 애매함: "상체를 세우면 무릎 부담이 증가해서…"
  - 비판인지 단순 궁금증인지 애매한 질문: "약안써도 저정도 가능함??", "7:37 이 발언.. 책임질수 있는거야..?"
운영자 대상이 아닌 것 (OK): 로이더·약물 얘기 속 몸·사람, 원댓글 작성자에게 한 말("니가 뭘 아냐"),
  운영자가 속하지 않은 집단(트레이너들·헬창들·의사들) 일반론, 이름이 명시된 제3자

▶ "명백" — 1회 즉시 차단 후보. 아래 세 조건을 전부 충족할 때 (대상 판단은 위 기준대로 정확하게)
  1. 대상이 운영자 본인임이 확실 (또는 운영자의 영상이 AI로 만들어졌다는 지적)
     - 위 기준으로 대상이 명확해야 함. 위 "모호" 사유가 하나라도 있으면 명백 아님
     - 영상 전용 규칙이 있으면 반드시 반영 (예: 근육선생 반박 영상에서 "의사" 비하는 운영자 대상이 아님)
     - 운영자가 영상에서 반박·비판하는 다른 사람이나 주장을 함께 비꼬는 댓글은 운영자 편이므로 명백 아님
       예: "승모 먼저하란 개소리는 심박하넼ㅋ 어좁 유튜버 피셜ㅋ" → "승모 먼저 하라"고 주장한 다른 유튜버를 비꼼 → 운영자 대상 아님
       "~하라는 사람", "~피셜", "~라던 유튜버"처럼 특정 주장을 한 제3자를 가리키면 운영자로 단정하지 말 것
  2. 운영자 본인을 비판·깎아내리는 말임 (명백한 비판이어야 함)
     - 말투는 무관: 정중해도, 조언 투여도, 비꼼이어도, 질문 형태로 비꼬아도 운영자를 비판하면 해당
     - 아닌 것: 농담·장난·친근한 놀림, 칭찬, 순수한 궁금증(비판 뉘앙스 없는 질문),
       영상·제품에 대한 피드백·불만, 영상 내용에 대한 정보 반박·토론
       (반박하면서 "안해보셨구나"처럼 가볍게 덧붙인 말도 피드백·반박으로 봄)
  3. 내용이 아래 유형 중 하나
     a. 몸·외모 비하 단정: "몸 구리네", "어깨 좁네", "가슴 작네", "어좁", "멸치네", "배 나왔네", "몸이 그게 뭐냐", "그 몸으로 뭘 가르쳐"
     b. 직업·자격 경멸: "약사 주제에", "약사 따위가", "약사가 뭘 안다고", "자격도 없는 게", "의사도 아닌 게",
        비꼬는 자격 의문: "하얀 가운은 왜 걸치는지 모르겠네요 자격 있는지", "인증도 못 받았으면서 전문가 행세",
        "가운 입으면 전문가냐" (질문 형태여도 비꼬는 투로 자격을 깎아내리면 명백)
     c. AI 단정·비판: "ai로 만든 영상", "ai로 딸깍", "gpt 돌려서 만든 거", "ai 목소리네", "ai가 대본 써줌", "이거 ai임", "몸 ai네"
     d. 운영자를 향한 직접 욕설·인신공격·조롱: "병신", "사기꾼", "꺼져", "개소리 하네",
        영상 속 운영자의 목소리·발음·말투·태도 조롱: "혀짧노 ㅋㅋ", "목소리 존나 거슬리네", "말 존나 많네 구질구질",
        "졸라 꺼드럭거리노 ㅋㅋ 암것도 못하는게"
     e. 운영자의 판단·능력·신뢰성·태도·동기, 영상을 만든 선택을 비판:
        "믿고 봤는데, 비속어 쓰니깐 신뢰도가 확 떨어지네", "굳이 이런 영상을 만들 이유가…ㅋ",
        "갈수록 보기가 불편해지네요 썸네일만 거창하고… 내용을 알차게 채울생각좀 하세요",
        "간지고 재미가 없어서 감점한다는게 무슨 말임? 주관 들으러 왔나", "걍 아무주제로 어플광고하는거임",
        "몸이 좀 더 커지면 설득력 있을 듯", "중급자가 근데 왜 어중간한 몸이에요",
        최상위 댓글로 영상 내용을 깎아내리며 신뢰성을 부정: "유사과학", "뇌피셜이네", "다좋아 진짜 개소리 ㅇㅇ"
        (대댓글이면 원댓글 내용을 두고 한 말일 수 있으니 원댓글을 확인)
  - 명백이 아닌 예:
    "ai 아님?" / "ai인 줄 ㅋㅋ 몸 너무 좋아서" (AI 질문·칭찬조 → 심각)
    "약사세요?" (단순 궁금증 → 경미)
    "어깨 좁은 사람한테도 효과 있나요?" (일반 질문 → OK)
    "저 사람 몸 별로네" (자료화면 속 다른 인물일 수 있음 → 경미)
    "근데 몸 ㅂ신인데 전완근만 좋으면 그냥 동물원에서 보는 쪼맨한 원숭이 같아요" (특정인이 아닌 일반적인 가정 → 경미)
    "너무 길어요", "1:12 편집실수있어요", "용어가 너무 어렵", "자막이 가려요", "뭐이리 어렵게 설명해",
    "왜 내용이 계속 똑같냐", "업로드 주기 왜이럼 편집자가 잃어버린 하드 다시 찾았나" (영상 피드백 → 경미)
    "상체를 세우면 무릎 부담이 증가해서…", "고블릿 스쿼트를 너무 과소평가하시네.. 안해보셨구나",
    "실패지점 가까워야만 근성장한다.....? 너무 옛날 정보 기반 아닌가?" (정보 반박·토론 → 경미)
  - 원칙: 대상이 운영자 본인으로 명확하고 운영자를 명백하게 비판하면 말투와 상관없이 명백.
    대상이 모호하거나(위 "모호" 사유), 영상·제품 피드백이나 정보 반박·토론이면 명백 아님 → 경미

▶ "심각" — AI 지적인데 명백이 아닌 것에만 씀 (2회면 차단)
- AI 관련 지적 전부(운영자/몸/영상이 AI다, AI 같다, AI로 만들었다, AI를 사용했다)는 주어가 없어도 최소 심각
  (단정·비판이면 명백, 질문·농담·칭찬조면 심각. AI 지적 댓글의 target 은 반드시 "운영자" 또는 "영상" 으로 표기할 것)
- AI 지적이 아닌 댓글에는 심각을 쓰지 말 것. 명백 조건을 충족하면 명백, 아니면 경미

▶ "경미" — 숨기지만 차단 근거는 아님
- 영상·제품 피드백과 불만: 길이, 편집, 자막, 설명 방식, 내용 반복, 업로드 주기, 앱 가격·기능 불만 등
  (대상이 운영자의 영상·제품으로 명확해도 명백한 비판이 아니면 경미)
- 영상 내용에 대한 정보 반박·토론 (가볍게 비꼬는 말이 섞여 있어도)
- 대상이 모호한 경우: 위 [대상이 운영자로 "명확"한지] 기준의 "모호" 사유에 해당해서
  "의심되면 HIDE" 원칙으로 숨긴 경우. 예: "하......", "등신 ㅇㅈㄹ", "그거 디스크가 비명지르는거임"
- 농담·장난·가벼운 반응: 공격 의도 없이 웃자고 한 말. 예: "형님 처음에 나오는 남자 엉덩이 보고 악플 달 뻔했습니다"
- 단순 궁금증: 비하 없이 묻기만 한 것. 예: "약사세요?", "뭐야 이분 직업이 약사였어? 😅", "예전 영상 어디 갔어요?"
- 호의적인 피드백: 칭찬과 함께 가볍게 바라는 점을 말한 것. 예: "센세 감사합니다. 책도 좀 리뉴얼…"
- target 이 "판단불가" 또는 "없음" 인데 HIDE 한 경우는 항상 경미

▶ 판단 순서: 명백 조건 세 가지를 전부 확실하게 충족하면 명백.
  아니면 AI 지적이면 심각, 그 밖에 숨길 댓글은 전부 경미.
▶ action 이 OK 이면 severity 는 "없음"

각 댓글마다 target(운영자/영상/제품/집단일반론/제3자/다른댓글러/본인/없음), action(HIDE/OK), severity(명백/심각/경미/없음),
ai_related(운영자·영상이 AI라거나 AI를 썼다는 지적이면 true, 아니면 false), reason(한 줄)을 응답하세요.
"""

# ──────────────────────────────────────────────────────────────────────────────
# ★ "명백" 2차 검수 프롬프트 ★
# 1차에서 "명백"으로 분류된 댓글만 별도 호출로 다시 확인. 여기서 확정돼야 1회 즉시 차단
# ──────────────────────────────────────────────────────────────────────────────
VERIFY_PROMPT = """
당신은 유튜브 채널 운영자 본인을 비판하는 댓글을 골라내는 2차 검수자입니다.
1차 판정에서 "명백(1회 즉시 차단)"으로 분류된 댓글들을 다시 검토합니다.
운영자의 방침: 대상이 운영자 본인으로 명확하고 운영자를 명백하게 비판하면 말투와 상관없이 1회 차단한다.
피드백·불만·정보 반박처럼 명백한 비판이 아니면 숨기기만 한다.
대신 대상 판단은 정확해야 한다. 다른 사람·다른 댓글러·반박 대상 유튜버를 향한 말을 운영자로 오인해 차단하면 안 된다.
아래 조건을 전부 충족하면 "명백", 아니면 "아님"으로 답하세요.
1차 판정을 그대로 믿지 말고 댓글 원문과 맥락을 직접 읽고 판단하세요.

[채널 운영자]
- 채널명 "급진적 과부하". 약사이자 운동 유튜버
- 운동 앱, 코칭 앱, 식단 앱, 운동 강의, 운동 책을 판매함

[영상 형식]
- 운영자 혼자 마이크 앞에서 대본을 읽으며 운동 이론을 설명하는 영상. 목소리·말은 전부 운영자 것
- 화면에 계속 나오는 사람은 운영자의 상반신뿐 (얼굴은 안 나옴)
- 옆에 다른 사람 자료화면(보디빌더·인플루언서 사진, 스톡 영상, 인스타·유튜브 운동 장면, 그림)을 넣음
- 운영자는 운동 시범을 하지 않고 보디빌더 몸도 아님. 운동 장면·자세·근육질 몸·로이더는 다른 사람

[명백 확정 조건 — 세 가지 모두 충족해야 함]
1. 대상: 운영자 본인임이 확실 (또는 운영자의 영상이 AI로 만들어졌다는 지적)
   - 운영자 대상이 명확한 것 (말투가 정중하거나 질문 형태여도 무관):
     목소리·발음·말투·말의 양·태도, 영상의 내용·주장·설명 (지칭이 없거나 타임스탬프가 있어도),
     상반신 몸·외모(어깨·가슴·팔·승모·등, 지칭 없는 "몸"), 얼굴을 안 보여주는 것에 대한 비꼼,
     영상을 만든 선택(이유·길이·편집·비속어·광고·예시 자료 선택), 운영자 호칭(이 사람·주인장·약사·형·쌤),
     최상위 댓글의 너/님, 운영자가 속한 집단(약사 유튜버·운동 유튜버·유튜버들·얼굴 안 까는 유튜버) 비판,
     운영자가 쓴 원댓글에 단 대댓글이나 @급진적_과부하 를 부르는 대댓글
   - 막연한 "다른 사람일 수도"는 근거가 아님. 아래처럼 구체적 단서가 있을 때만 아님:
     자료화면 속 사람일 수 있는 몸·자세("저 사람", "쟤", "저건", "예시", 타임스탬프 + 몸·자세, 하체, 운동 자세·동작),
     로이더·약물 얘기, 영상에서 다루는 운동 방식을 하는 사람들 비판("레그프레스 깔짝대는 사람들 극혐"),
     운영자가 속하지 않은 집단(트레이너들·헬창들) 일반론, 누구에게 한 말인지 아무 단서 없음("등신 ㅇㅈㄹ"),
     작성자 본인 얘기
   - 대댓글이면 원댓글·앞선 답글을 읽고 누구 얘기인지 확인. 원댓글 작성자에게 한 말("니가 뭘 아냐")이면 아님
   - 영상 전용 규칙이 함께 주어지면 반드시 반영
   - 운영자가 영상에서 반박하는 다른 사람·주장을 함께 비꼬는 댓글이면 아님
     ("승모 먼저하란 개소리는 심박하넼ㅋ 어좁 유튜버 피셜ㅋ" → 그 주장을 한 다른 유튜버 비판 → 아님)
2. 내용: 운영자 본인을 비판·깎아내리는 말
   - 말투는 무관: 정중해도, 조언 투여도, 비꼼이어도, 질문 형태로 비꼬아도 운영자를 비판하면 해당
   - 아닌 것: 농담·장난·친근한 놀림("형님 ㅋㅋ"), 칭찬, 순수한 궁금증(비판 뉘앙스 없는 질문)
3. 유형: 아래 중 하나에 해당
   a. 몸·외모 비하 단정 ("몸 구리네", "어깨 좁네", "가슴 작네", "멸치", "그 몸으로 뭘 가르쳐")
   b. 직업·자격 경멸 ("약사 주제에", "약사 따위가", "약사가 뭘 안다고", "자격도 없는 게"),
      비꼬는 자격 의문 ("하얀 가운은 왜 걸치는지 모르겠네요 자격 있는지", "인증도 못 받았으면서 전문가 행세")
   c. AI 단정·비판 ("ai로 만든 영상", "ai로 딸깍", "gpt 돌려서 만든 거", "ai 목소리", "이거 ai임")
      단, "ai 아님?" 같은 질문이나 "ai인 줄 ㅋㅋ 몸 좋아서" 같은 칭찬조는 아님
   d. 운영자를 향한 직접 욕설·인신공격·조롱 ("병신", "사기꾼", "꺼져"),
      영상 속 운영자의 목소리·발음·말투·태도 조롱 ("혀짧노 ㅋㅋ", "목소리 존나 거슬리네", "말 존나 많네 구질구질",
      "졸라 꺼드럭거리노 ㅋㅋ 암것도 못하는게")
   e. 운영자의 판단·능력·신뢰성·태도·동기, 영상을 만든 선택에 대한 비판
      ("비속어 쓰니깐 신뢰도가 확 떨어지네", "굳이 이런 영상을 만들 이유가…ㅋ", "썸네일만 거창하고 내용 좀 채우세요",
       "주관 들으러 왔나", "걍 아무주제로 어플광고하는거임", "몸이 좀 더 커지면 설득력 있을 듯", "왜 어중간한 몸이에요",
       최상위 댓글로 영상 내용을 깎아내리며 신뢰성을 부정하는 "유사과학", "뇌피셜이네")
   - 아님: 영상·제품 피드백과 불만("너무 길어요", "편집실수 있어요", "뭐이리 어렵게 설명해", "왜 내용이 계속 똑같냐",
     "업로드 주기 왜이럼"), 영상 내용에 대한 정보 반박·토론("고블릿 스쿼트를 너무 과소평가하시네.. 안해보셨구나"),
     특정인이 아닌 일반적인 가정("몸 ㅂ신인데 전완근만 좋으면 원숭이 같아요")

각 댓글마다 verdict("명백" 또는 "아님")와 reason(한 줄)을 응답하세요.
"""

VERIFY_SCHEMA = {
    "type": "object",
    "properties": {
        "results": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "integer"},
                    "verdict": {"type": "string", "enum": ["명백", "아님"]},
                    "reason": {"type": "string"},
                },
                "required": ["id", "verdict", "reason"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["results"],
    "additionalProperties": False,
}
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
    title = (c.get("video_title") or "").strip()
    if title:
        lines.append(f'  영상 제목: {title}')
    parent = (c.get("parent_text") or "").strip()
    if parent:
        who = "원댓글(운영자 작성)" if c.get("parent_is_operator") else "원댓글"
        lines.append(f'  {who}: {parent[:PARENT_TEXT_MAX_CHARS]}')
    prev = [p.strip() for p in (c.get("prev_replies") or []) if p and p.strip()]
    if prev:
        lines.append('  앞선 답글들:')
        for p in prev:
            lines.append(f'    - {p[:PREV_REPLY_MAX_CHARS]}')
    lines.append(f'  댓글: {c["text"][:500]}')
    return "\n".join(lines)

def ai_judge_batch(client: anthropic.Anthropic, comments: list[dict], extra_rules: str = "") -> list[dict]:
    """extra_rules 가 있으면 기본 프롬프트 뒤에 붙여서 그 배치에만 적용."""
    system_prompt = MODERATION_PROMPT
    if extra_rules:
        system_prompt = MODERATION_PROMPT.rstrip() + "\n\n" + extra_rules.strip() + "\n"
    comment_list = "\n\n".join(format_comment_for_ai(c) for c in comments)
    user_message = (
        "아래 댓글들을 판단해주세요. "
        "댓글마다 먼저 부정의 대상이 운영자인지 아닌지 정한 뒤 HIDE/OK를 결정하세요.\n\n"
        f"{comment_list}"
    )

    raw = ""
    try:
        response = client.messages.create(
            model=AI_MODEL,
            max_tokens=8000,
            system=system_prompt,
            messages=[{"role": "user", "content": user_message}],
            output_config={
                "effort": AI_EFFORT,
                "format": {"type": "json_schema", "schema": RESULT_SCHEMA},
            },
        )

        if response.stop_reason == "refusal":
            log.error("AI가 판단을 거부함 (refusal) — 이 배치는 건너뜀")
            return []

        # 응답에는 thinking 블록이 먼저 올 수 있으므로 text 블록만 모음
        raw = "".join(b.text for b in response.content if b.type == "text").strip()
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

def classify_level(result: dict, verdict: str) -> str:
    """AI 판정 1건 + 2차 검수 결과 → 처리 등급 (OK / 명백 / 심각 / 심각(명백 기각) / 경미).
    운영자에 대한 명백한 비판(2차 검수 통과)만 1회 차단. 2회 차단(심각)은 AI 지적에만 쓰고,
    그 밖에 숨기는 댓글은 AI 가 심각이라고 해도 경미(숨김만)로 내림."""
    if result.get("action") != "HIDE":
        return "OK"
    severity = result.get("severity", "경미")
    if severity == "명백" and verdict == "명백":
        return "명백"
    # 대상이 모호하면(판단불가/없음) 차단 근거로 쓰지 않음
    target_clear = result.get("target", "") not in ("판단불가", "없음", "")
    if severity in ("명백", "심각") and target_clear and result.get("ai_related") is True:
        return "심각(명백 기각)" if severity == "명백" and verdict == "아님" else "심각"
    return "경미"

def ai_verify_blatant(client: anthropic.Anthropic, candidates: list[dict], extra_rules: str = "") -> dict[int, tuple[str, str]]:
    """1차에서 "명백"으로 분류된 댓글을 엄격하게 다시 검수.
    반환: {id: (verdict, reason)}. 호출 실패 시 빈 dict → 아무도 즉시 차단되지 않음 (안전한 쪽으로 실패)."""
    system_prompt = VERIFY_PROMPT
    if extra_rules:
        system_prompt = VERIFY_PROMPT.rstrip() + "\n\n[참고: 이 영상의 전용 규칙]\n" + extra_rules.strip() + "\n"
    blocks = []
    for c in candidates:
        blocks.append(format_comment_for_ai(c) + f'\n  1차 판정 사유: {c.get("first_reason", "")}')
    user_message = "아래 댓글들이 운영자 본인을 비판하는 댓글인지 대상과 내용을 검수해주세요.\n\n" + "\n\n".join(blocks)

    raw = ""
    try:
        response = client.messages.create(
            model=AI_MODEL,
            max_tokens=4000,
            system=system_prompt,
            messages=[{"role": "user", "content": user_message}],
            output_config={
                "effort": AI_EFFORT,
                "format": {"type": "json_schema", "schema": VERIFY_SCHEMA},
            },
        )
        if response.stop_reason == "refusal":
            log.error("2차 검수 거부됨 (refusal) — 즉시 차단 없이 진행")
            return {}
        raw = "".join(b.text for b in response.content if b.type == "text").strip()
        parsed = json.loads(raw)
        return {
            r["id"]: (r.get("verdict", "아님"), r.get("reason", ""))
            for r in parsed.get("results", [])
            if isinstance(r.get("id"), int)
        }
    except json.JSONDecodeError as e:
        log.error(f"2차 검수 응답 파싱 실패: {e}\n응답: {raw[:200]}")
        return {}
    except anthropic.APIError as e:
        log.error(f"2차 검수 API 오류: {e}")
        return {}

def hide_comment(youtube, comment_id: str):
    youtube.comments().setModerationStatus(
        id=comment_id,
        moderationStatus="heldForReview"
    ).execute()

def reject_and_ban_author(youtube, comment_id: str):
    """댓글을 거부 처리하면서 작성자를 채널에서 숨김 (banAuthor 는 rejected 에서만 유효)."""
    youtube.comments().setModerationStatus(
        id=comment_id,
        moderationStatus="rejected",
        banAuthor=True
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
    """영상 ID 목록 → {video_id: {"title"}}. 50개씩 일괄 조회."""
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
            info[item["id"]] = {"title": sn.get("title", "")}
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
        # 원댓글 작성자가 채널 주인(운영자)인지: 스레드의 채널 ID 와 작성자 채널 ID 비교
        owner_channel = snippet.get("channelId") or top_snippet.get("channelId", "")
        top_is_operator = bool(owner_channel) and \
            top_snippet.get("authorChannelId", {}).get("value", "") == owner_channel
        top_comment["_is_reply"] = False
        top_comment["_is_pinned_reply"] = False
        top_comment["_video_id"] = video_id
        top_comment["_parent_text"] = ""
        normal_top.append(top_comment)

        # 대댓글이 있으면 가져오기 (원댓글 본문을 함께 붙여서 대상 판단에 활용)
        if reply_count > 0:
            thread_id = top_comment["id"]
            replies = get_replies(youtube, thread_id, is_pinned=thread.get("_is_pinned", False))
            prev_texts = []
            for r in replies:
                r["_video_id"] = r.get("snippet", {}).get("videoId") or video_id
                r["_parent_text"] = top_text
                r["_parent_is_operator"] = top_is_operator
                r["_prev_replies"] = prev_texts[-PREV_REPLY_COUNT:]
                r_text = r.get("snippet", {}).get("textOriginal", r.get("snippet", {}).get("textDisplay", ""))
                prev_texts = prev_texts + [r_text]
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
    log.info(f"AI 모델: {AI_MODEL} (effort={AI_EFFORT})")

    youtube = get_authenticated_service()
    ai = get_anthropic_client()
    data = load_data()

    hidden_count = 0
    banned_count = 0
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

        # 이미 처리한 댓글 제외 (MODBOT_REJUDGE=true 면 전부 다시 판단)
        rejudge = os.environ.get("MODBOT_REJUDGE", "").strip().lower() == "true"
        if rejudge:
            log.info("MODBOT_REJUDGE=true: 처리 완료 목록을 무시하고 전부 다시 판단")
            new_comments = list(all_comments)
        else:
            processed_set = set(data["processed"])
            new_comments = [c for c in all_comments if c["id"] not in processed_set]
        log.info(f"새 댓글 {len(new_comments)}개 판단 필요 (이미 처리 {len(all_comments) - len(new_comments)}개 건너뜀)")

        # 영상별 전용 규칙이 있는 댓글은 따로 묶어서 그 규칙으로 판단 (기본 그룹 키: "")
        groups: dict[str, list[dict]] = {}
        for c in new_comments:
            key = c.get("_video_id", "") if c.get("_video_id", "") in VIDEO_SPECIFIC_RULES else ""
            groups.setdefault(key, []).append(c)
        for key, cs in groups.items():
            if key:
                log.info(f"전용 규칙 적용 영상 {key} ({VIDEO_SPECIFIC_RULES[key]['title']}): 댓글 {len(cs)}개")

        batches = []  # (group_key, batch_comments)
        for key in sorted(groups, key=lambda k: (k != "", k)):
            cs = groups[key]
            for batch_start in range(0, len(cs), AI_BATCH_SIZE):
                batches.append((key, cs[batch_start: batch_start + AI_BATCH_SIZE]))

        done_count = 0
        for batch_no, (group_key, batch) in enumerate(batches):
            extra_rules = VIDEO_SPECIFIC_RULES[group_key]["rules"] if group_key else ""

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
                    "parent_text": comment.get("_parent_text", ""),
                    "parent_is_operator": comment.get("_parent_is_operator", False),
                    # 앞선 답글은 전용 규칙 영상에서만 전달 (다른 영상 동작 불변)
                    "prev_replies": comment.get("_prev_replies", []) if group_key else [],
                })
                comment_map[i] = comment

            scanned_count += len(batch)

            tag = f" [전용 규칙: {group_key}]" if group_key else ""
            log.info(f"AI 판단 중... ({done_count+1}~{done_count+len(batch)}개){tag}")
            done_count += len(batch)
            results = ai_judge_batch(ai, ai_input, extra_rules=extra_rules)
            ai_call_count += 1

            # "명백" 후보는 2차 검수를 거친 뒤에만 즉시 차단
            input_map = {d["id"]: d for d in ai_input}
            blatant_candidates = []
            for r in results:
                rid = r.get("id")
                if (
                    r.get("action") == "HIDE"
                    and r.get("severity") == "명백"
                    and r.get("target") in ("운영자", "영상")
                    and rid in input_map
                    and rid in comment_map
                    and comment_map[rid]["id"] not in NEVER_HIDE_IDS
                ):
                    blatant_candidates.append({**input_map[rid], "first_reason": r.get("reason", "")})
            verdicts: dict[int, tuple[str, str]] = {}
            if blatant_candidates and AUTO_BAN and BLATANT_INSTANT_BAN:
                log.info(f"명백 후보 {len(blatant_candidates)}개 2차 검수 중...")
                verdicts = ai_verify_blatant(ai, blatant_candidates, extra_rules=extra_rules)
                ai_call_count += 1

            for result in results:
                idx = result.get("id")
                action = result.get("action", "OK")
                target = result.get("target", "")
                severity = result.get("severity", "경미")
                verdict, verify_reason = verdicts.get(idx, ("", ""))
                level = classify_level(result, verdict)
                is_blatant = level == "명백"
                is_severe = level in ("명백", "심각", "심각(명백 기각)")
                reason = result.get("reason", "")
                if target:
                    reason = f"[대상:{target}] {reason}"
                if action == "HIDE":
                    if is_blatant:
                        reason = f"{reason} / 2차 검수: {verify_reason}"
                    elif severity == "명백" and verdict == "아님":
                        reason = f"{reason} / 2차 기각: {verify_reason}"
                    reason = f"[{level}] {reason}"

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

                if action == "HIDE" and comment_id in NEVER_HIDE_IDS:
                    log.info(f"건너뜀 (예외 목록): @{author_name} | \"{text[:40]}...\"")
                    continue

                if action == "HIDE":
                    if author_channel_id not in data["offenders"]:
                        data["offenders"][author_channel_id] = {
                            "name": author_name,
                            "channel_id": author_channel_id,
                            "count": 0,
                            "last_comment": "",
                            "last_reason": "",
                            "timestamps": [],
                            "severe_count": 0,
                            "banned": False,
                            "banned_at": None,
                            "channel_url": f"https://www.youtube.com/channel/{author_channel_id}"
                        }
                    offender = data["offenders"][author_channel_id]
                    new_count = offender["count"] + 1
                    new_severe = offender.get("severe_count", 0) + (1 if is_severe else 0)
                    can_ban = (
                        AUTO_BAN
                        and not offender.get("banned", False)
                        and author_channel_id != "unknown"
                    )
                    ban_by_blatant = can_ban and BLATANT_INSTANT_BAN and is_blatant
                    ban_by_severe = can_ban and is_severe and new_severe >= BAN_THRESHOLD_SEVERE
                    should_ban = ban_by_blatant or ban_by_severe
                    tag = "📌대댓글" if is_pinned_reply else "↩대댓글" if comment.get("_is_reply") else "💬댓글"

                    try:
                        if should_ban:
                            reject_and_ban_author(youtube, comment_id)
                            ban_reason = "명백 1회" if ban_by_blatant else f"심각 {new_severe}회"
                            offender["banned"] = True
                            offender["banned_at"] = datetime.now().isoformat()
                            offender["ban_reason"] = ban_reason
                            offender["ban_comment"] = text[:200]
                            banned_count += 1
                            log.info(f"🚫 차단 {tag}: @{author_name} ({ban_reason} / 전체 {new_count}회) | {reason} | \"{text[:40]}...\"")
                        else:
                            hide_comment(youtube, comment_id)
                            log.info(f"숨김 {tag}: @{author_name} | {reason} | \"{text[:40]}...\"")
                        hidden_count += 1

                        offender["count"] = new_count
                        offender["severe_count"] = new_severe
                        offender["name"] = author_name
                        offender["last_comment"] = text[:100]
                        offender["last_reason"] = reason
                        offender["timestamps"].append(datetime.now().isoformat())
                        time.sleep(0.3)

                    except HttpError as e:
                        log.error(f"숨김/차단 실패 ({comment_id}): {e}")

            if batch_no + 1 < len(batches):
                time.sleep(1)

        data["stats"]["total_hidden"] += hidden_count
        data["stats"]["total_banned"] = data["stats"].get("total_banned", 0) + banned_count
        data["stats"]["total_scanned"] += scanned_count
        data["stats"]["ai_calls"] = data["stats"].get("ai_calls", 0) + ai_call_count
        data["stats"]["last_run"] = datetime.now().isoformat()

        if len(data["processed"]) > 10000:
            data["processed"] = data["processed"][-10000:]

        save_data(data)

        repeat_offenders = {
            cid: info for cid, info in data["offenders"].items()
            if info["count"] >= REPEAT_THRESHOLD or info.get("banned")
        }

        log.info(f"\n{'='*50}")
        log.info(f"결과: {scanned_count}개 스캔 → {hidden_count}개 숨김 (이 중 차단 {banned_count}명, AI 호출 {ai_call_count}회)")
        log.info(f"누적: 총 {data['stats']['total_hidden']}개 숨김, 차단 {data['stats'].get('total_banned', 0)}명, AI {data['stats']['ai_calls']}회 호출")

        if repeat_offenders:
            log.info(f"\n⚠️  반복 악플러 {len(repeat_offenders)}명:")
            for cid, info in sorted(repeat_offenders.items(), key=lambda x: -x[1]["count"]):
                flag = " [차단됨]" if info.get("banned") else ""
                log.info(f"  → {info['name']} (전체 {info['count']}회, 심각 {info.get('severe_count', 0)}회){flag} | {info['channel_url']}")
        else:
            log.info("반복 악플러 없음")

    except HttpError as e:
        log.error(f"YouTube API 오류: {e}")
        raise

    return {
        "scanned": scanned_count,
        "hidden": hidden_count,
        "banned": banned_count,
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
