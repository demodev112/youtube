#!/usr/bin/env python3
"""
GitHub Actions용 Google 토큰 생성 스크립트
딱 한 번만 로컬에서 실행하면 됩니다.
출력된 JSON을 GitHub Secret에 붙여넣으세요.
"""

from google_auth_oauthlib.flow import InstalledAppFlow
from pathlib import Path
import json

SCOPES = ["https://www.googleapis.com/auth/youtube.force-ssl"]

if not Path("client_secrets.json").exists():
    print("❌ client_secrets.json 파일이 없습니다.")
    print("Google Cloud Console에서 다운로드 후 이 폴더에 넣으세요.")
    exit(1)

print("브라우저가 열립니다. YouTube 채널 계정으로 로그인하세요...\n")
flow = InstalledAppFlow.from_client_secrets_file("client_secrets.json", SCOPES)
creds = flow.run_local_server(port=0)

token_json = creds.to_json()

# 파일로도 저장
with open("token.json", "w") as f:
    f.write(token_json)

print("\n✅ 완료! 아래 내용을 복사해서 GitHub Secret에 붙여넣으세요:")
print("   Secret 이름: GOOGLE_TOKEN_JSON")
print("=" * 60)
print(token_json)
print("=" * 60)
print("\nGitHub Secret 추가 방법:")
print("1. GitHub 레포 → Settings → Secrets and variables → Actions")
print("2. New repository secret")
print("3. Name: GOOGLE_TOKEN_JSON")
print("4. Value: 위의 JSON 전체 붙여넣기")
