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

REPEAT_THRESHOLD = 3
AI_BATCH_SIZE = 20
PARENT_TEXT_MAX_CHARS = 300  # AI에 넘길 원댓글 길이
PREV_REPLY_MAX_CHARS = 200   # 전용 규칙 영상에서 AI에 넘길 앞선 답글 1개당 길이
PREV_REPLY_COUNT = 3         # 전용 규칙 영상에서 AI에 넘길 앞선 답글 개수

AI_MODEL = "claude-opus-5-5"
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
- 근육선생의 주장: 체리피킹한 연구 몇 개를 근거로, 케이블 레터럴 레이즈는 어깨에 위험하고
  삼각근이 아니라 회전근개만 쓰게 돼서 근성장에도 좋지 않으니 덤벨 레터럴 레이즈를 하라.
- 운영자(급진적 과부하, 약사)의 입장: 위 주장을 반박함. 케이블 레터럴 레이즈는 문제없다.
- 용어: "케이블 레터럴 레이즈" = "케이블 사레레" = "케사레" = "케이블 사이드 레터럴 레이즈" = "케이블 레레" (전부 같은 운동)
  "덤벨 레터럴 레이즈" = "덤벨 사레레" = "덤사레"

★ 케이블 사레레는 우리 편: 케이블 사레레가 좋다/문제없다/효과 있다고 옹호하는 댓글은
  욕설·비속어가 섞여 있어도 무조건 OK. 운영자의 입장과 같은 편이기 때문.
  (일반 규칙의 "주어 없는 부정/욕설 → HIDE"보다 이 규칙이 우선)
  예: "케이블 사레레 ㅈㄴ 좋은데 뭔 개소리야" → 케이블 옹호 → OK
      "케이블 위험하다는 새끼들 운동 안 해본 티 남" → 케이블 옹호 → OK
      "씨발 케사레 하면 어깨 터지는데 뭐가 위험함" → 케이블 옹호 (반어) → OK

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
- "의사가 약사보다 똑똑하니까 그 사람이 맞음" → 근육선생 편 + 운영자 깎아내림 → HIDE
- "급진적 과부하가 뭘 안다고 의사한테 훈수냐" → 운영자 비판 → HIDE
- "약사가 정형외과 의사한테 어깨 얘기를 해?" → 운영자 비판 → HIDE
- "둘 다 어그로 끄는 거 같은데" → 둘 다 비판 → HIDE
- "별로네" (단서 없음) → 대상 판단 불가 → OK

target 값은 "운영자" / "근육선생" / "둘다" / "판단불가" / "없음" 중 하나로 응답하세요.
""",
    },
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
                    "reason": {"type": "string"},
                },
                "required": ["id", "target", "action", "reason"],
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
- 직업: 약사이자 유튜버
- 판매 제품: 운동 앱, 코칭 앱, 식단 앱, 운동 강의, 운동 책
- 본인이 영상에 직접 출연함 (영상에 나오는 사람 = 운영자)
- 모든 댓글은 이 운영자의 유튜브 영상에 달린 것임

[입력 형식]
- 영상 제목: 참고용. 없거나 도움이 안 될 수 있으니 댓글 본문만으로도 판단할 것
- 원댓글: 대댓글인 경우 그 대댓글이 답하고 있는 원래 댓글
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
- 예외는 딱 하나: 운영자/영상과 전혀 무관하게 AI를 도구로 언급하는 경우만 OK
  예: "운동 앱에 AI 기능 있나요?", "챗GPT한테 물어봤더니"
  → 조금이라도 애매하면 HIDE

[대상 판단 — "이 댓글의 부정이 운영자(또는 운영자의 영상/제품)를 향하는가?"]
운영자를 향할 가능성이 있으면 바로 HIDE. 100% 확실할 필요 없음. 의심되면 HIDE.

▶ 아래는 100% 운영자를 가리키는 말. 하나라도 있고 내용이 부정적이면 무조건 HIDE:
- 단수 지시어: "얘", "쟤", "이 사람", "저 사람", "이분", "저분", "이 양반", "이 놈", "이 새끼"
- 역할 호칭: "주인장", "채널 주인", "채널주", "이 유튜버", "유튜버", "영상 올린 사람", "영상 주인", "운영자", "작성자"
- 직업 호칭: "약사", "약사님", "약사가", "약사 주제에", "약사면서"
- 2인칭/친근 호칭: "너", "니", "네가", "당신", "님", "형", "누나", "언니", "오빠", "선생님", "쌤"
- 주어 없이 운영자에게 직접 말하는 명령/반말/질문: "공부 좀 해라", "그만해라", "닥쳐", "직접 해보긴 했냐", "뭘 안다고", "왜 이러냐"
- "이 영상", "이 채널", "이 앱", "이 강의", "이 책", "여기" 처럼 운영자의 것을 가리키는 말

▶ 주어가 아예 없는 부정적 댓글 → 운영자/영상을 향한 것으로 간주 → HIDE
  예: "별로네", "실망", "사기 같은데", "틀린 정보임", "몸 보면 앎", "너무 길다", "비싸다"

▶ 운영자가 아닌 대상 → OK (내용이 거칠어도 숨기지 않음):
- 복수/집단 일반론: "트레이너들", "헬창들", "요즘 유튜버들", "의사들", "피티쌤들", "헬스장 사람들", "업계", "사람들", "요즘 애들"
  예: "트레이너들 요즘 다 사기꾼", "헬창들 요즘 너무 별로", "유튜버들 다 광고충" → OK
  단, 그 집단 비판이 운영자의 자격을 부정하는 데 쓰이면 운영자: "약사들이 운동을 뭘 알아?", "약사 따위가" → HIDE
  단, "이 사람도", "여기도", "너도", "이 채널도" 처럼 운영자를 그 집단에 포함시키면 → HIDE
- 이름이 명시된 다른 유튜버/유명인/트레이너: "○○ 트레이너 별로", "□□ 채널이 더 낫다"(비교로 운영자를 깎으면 HIDE)
- 대댓글이 원댓글 작성자를 향하는 경우: 원댓글 의견 반박, 원댓글 작성자 비난 → OK
  단, 그 과정에서 운영자/영상/제품을 함께 깎아내리면 HIDE
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
5. 트레이너들/헬창들/유튜버들 같은 집단 일반론, 이름 명시된 제3자, 원댓글 작성자, 본인, 단순 비속어 → OK

각 댓글마다 target(운영자/영상/제품/집단일반론/제3자/다른댓글러/본인/없음), action(HIDE/OK), reason(한 줄)을 응답하세요.
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
    title = (c.get("video_title") or "").strip()
    if title:
        lines.append(f'  영상 제목: {title}')
    parent = (c.get("parent_text") or "").strip()
    if parent:
        lines.append(f'  원댓글: {parent[:PARENT_TEXT_MAX_CHARS]}')
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

            if batch_no + 1 < len(batches):
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
