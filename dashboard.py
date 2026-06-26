#!/usr/bin/env python3
"""
악플 모더레이션 대시보드
flask로 실행: python dashboard.py
브라우저에서 http://localhost:5000 접속
"""

from flask import Flask, jsonify, render_template_string
import json
from pathlib import Path

app = Flask(__name__)
DATA_FILE = "offender_data.json"
REPEAT_THRESHOLD = 3

TEMPLATE = """
<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>YouTube 악플 관리 대시보드</title>
<style>
  @import url('https://fonts.googleapis.com/css2?family=IBM+Plex+Sans+KR:wght@400;500;600&family=IBM+Plex+Mono&display=swap');

  :root {
    --bg: #0f0f10;
    --surface: #1a1a1e;
    --surface2: #242428;
    --border: rgba(255,255,255,0.08);
    --border2: rgba(255,255,255,0.14);
    --text: #f0ede8;
    --muted: #8a8882;
    --red: #e24b4a;
    --red-bg: #2a1515;
    --amber: #ef9f27;
    --amber-bg: #251a08;
    --green: #639922;
    --green-bg: #141f08;
    --accent: #7f77dd;
  }

  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: 'IBM Plex Sans KR', sans-serif; background: var(--bg); color: var(--text); min-height: 100vh; }

  .top-bar {
    border-bottom: 1px solid var(--border);
    padding: 16px 32px;
    display: flex;
    align-items: center;
    justify-content: space-between;
    position: sticky; top: 0;
    background: rgba(15,15,16,0.95);
    backdrop-filter: blur(8px);
    z-index: 10;
  }
  .top-bar h1 { font-size: 15px; font-weight: 500; letter-spacing: -0.01em; }
  .top-bar h1 span { color: var(--red); }
  .refresh-btn {
    background: var(--surface2);
    border: 1px solid var(--border2);
    color: var(--text);
    padding: 6px 14px;
    border-radius: 6px;
    cursor: pointer;
    font-size: 13px;
    font-family: inherit;
    transition: background 0.15s;
  }
  .refresh-btn:hover { background: var(--border2); }

  .container { max-width: 960px; margin: 0 auto; padding: 32px 24px; }

  .stats-grid {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
    gap: 12px;
    margin-bottom: 36px;
  }
  .stat-card {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 10px;
    padding: 16px 18px;
  }
  .stat-card .label {
    font-size: 11px;
    text-transform: uppercase;
    letter-spacing: 0.06em;
    color: var(--muted);
    margin-bottom: 8px;
  }
  .stat-card .value {
    font-size: 28px;
    font-weight: 600;
    font-family: 'IBM Plex Mono', monospace;
    letter-spacing: -0.02em;
  }
  .stat-card .value.red { color: var(--red); }
  .stat-card .value.amber { color: var(--amber); }
  .stat-card .value.green { color: var(--green); }

  .section-header {
    display: flex;
    align-items: center;
    gap: 10px;
    margin-bottom: 16px;
  }
  .section-header h2 {
    font-size: 13px;
    font-weight: 500;
    text-transform: uppercase;
    letter-spacing: 0.08em;
    color: var(--muted);
  }
  .badge {
    font-size: 11px;
    font-family: 'IBM Plex Mono', monospace;
    background: var(--red-bg);
    color: var(--red);
    border: 1px solid rgba(226,75,74,0.25);
    border-radius: 4px;
    padding: 2px 7px;
  }
  .badge.warn {
    background: var(--amber-bg);
    color: var(--amber);
    border-color: rgba(239,159,39,0.25);
  }

  .offender-list { display: flex; flex-direction: column; gap: 8px; }
  .offender-card {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 10px;
    padding: 14px 18px;
    display: flex;
    align-items: flex-start;
    justify-content: space-between;
    gap: 16px;
    transition: border-color 0.15s;
  }
  .offender-card:hover { border-color: var(--border2); }
  .offender-card.high { border-left: 2px solid var(--red); }
  .offender-card.med { border-left: 2px solid var(--amber); }

  .offender-info { flex: 1; min-width: 0; }
  .offender-name {
    font-size: 14px;
    font-weight: 500;
    margin-bottom: 4px;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }
  .offender-comment {
    font-size: 12px;
    color: var(--muted);
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    max-width: 480px;
  }

  .offender-meta {
    display: flex;
    flex-direction: column;
    align-items: flex-end;
    gap: 8px;
    flex-shrink: 0;
  }
  .count-pill {
    font-family: 'IBM Plex Mono', monospace;
    font-size: 12px;
    padding: 3px 10px;
    border-radius: 20px;
    font-weight: 500;
  }
  .count-pill.high { background: var(--red-bg); color: var(--red); }
  .count-pill.med { background: var(--amber-bg); color: var(--amber); }

  .ban-btn {
    font-size: 11px;
    padding: 5px 10px;
    border-radius: 5px;
    border: 1px solid var(--border2);
    background: transparent;
    color: var(--muted);
    cursor: pointer;
    font-family: inherit;
    white-space: nowrap;
    text-decoration: none;
    display: inline-block;
    transition: all 0.15s;
  }
  .ban-btn:hover { background: var(--red-bg); color: var(--red); border-color: rgba(226,75,74,0.3); }

  .empty-state {
    text-align: center;
    padding: 48px 24px;
    color: var(--muted);
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 10px;
    font-size: 14px;
  }
  .empty-state .icon { font-size: 32px; margin-bottom: 12px; }

  .last-run {
    font-size: 11px;
    color: var(--muted);
    text-align: right;
    margin-top: 24px;
    font-family: 'IBM Plex Mono', monospace;
  }

  .divider { height: 1px; background: var(--border); margin: 32px 0; }

  .how-to-ban {
    background: var(--amber-bg);
    border: 1px solid rgba(239,159,39,0.2);
    border-radius: 10px;
    padding: 16px 20px;
    font-size: 13px;
    color: var(--amber);
    line-height: 1.7;
    margin-top: 12px;
  }
  .how-to-ban strong { font-weight: 600; }
</style>
</head>
<body>

<div class="top-bar">
  <h1>YouTube <span>악플 관리</span> 대시보드</h1>
  <button class="refresh-btn" onclick="location.reload()">↻ 새로고침</button>
</div>

<div class="container">
  <div class="stats-grid" id="stats-grid">
    <div class="stat-card">
      <div class="label">총 숨긴 댓글</div>
      <div class="value red" id="total-hidden">—</div>
    </div>
    <div class="stat-card">
      <div class="label">총 스캔</div>
      <div class="value" id="total-scanned">—</div>
    </div>
    <div class="stat-card">
      <div class="label">반복 악플러</div>
      <div class="value amber" id="repeat-count">—</div>
    </div>
    <div class="stat-card">
      <div class="label">고위험 (5회+)</div>
      <div class="value red" id="high-risk-count">—</div>
    </div>
  </div>

  <div class="section-header">
    <h2>반복 악플러</h2>
    <span class="badge" id="offender-badge">0명</span>
  </div>
  <p style="font-size: 13px; color: var(--muted); margin-bottom: 16px;">
    {{ threshold }}회 이상 숨김 처리된 유저입니다. 직접 채널 방문해서 <strong style="color:var(--text)">영구 숨기기</strong>하세요.
  </p>

  <div class="offender-list" id="offender-list">
    <div class="empty-state">
      <div class="icon">✓</div>
      데이터 로딩 중...
    </div>
  </div>

  <div class="how-to-ban">
    <strong>영구 숨기기 방법:</strong> 링크 클릭 → 유저 채널 → 댓글 아무거나 클릭 → 점 3개 메뉴 → <strong>"사용자 숨기기"</strong>
  </div>

  <div class="last-run" id="last-run"></div>
</div>

<script>
async function loadData() {
  try {
    const res = await fetch('/api/data');
    const d = await res.json();

    document.getElementById('total-hidden').textContent = d.stats.total_hidden.toLocaleString();
    document.getElementById('total-scanned').textContent = d.stats.total_scanned.toLocaleString();
    document.getElementById('repeat-count').textContent = d.repeat_offenders.length;
    document.getElementById('high-risk-count').textContent = d.high_risk;
    document.getElementById('offender-badge').textContent = d.repeat_offenders.length + '명';

    if (d.stats.last_run) {
      document.getElementById('last-run').textContent = '마지막 실행: ' + new Date(d.stats.last_run).toLocaleString('ko-KR');
    }

    const list = document.getElementById('offender-list');
    if (d.repeat_offenders.length === 0) {
      list.innerHTML = '<div class="empty-state"><div class="icon">🛡</div>반복 악플러가 없습니다</div>';
      return;
    }

    list.innerHTML = d.repeat_offenders.map(o => {
      const isHigh = o.count >= 5;
      const cls = isHigh ? 'high' : 'med';
      const lastTs = o.timestamps.length > 0
        ? new Date(o.timestamps[o.timestamps.length-1]).toLocaleDateString('ko-KR')
        : '';
      return `
        <div class="offender-card ${cls}">
          <div class="offender-info">
            <div class="offender-name">${escHtml(o.name)}</div>
            <div class="offender-comment">${o.last_comment ? '"' + escHtml(o.last_comment) + '"' : ''}</div>
          </div>
          <div class="offender-meta">
            <span class="count-pill ${cls}">${o.count}회 숨김</span>
            <a href="${o.channel_url}" target="_blank" class="ban-btn">채널 방문 →</a>
          </div>
        </div>
      `;
    }).join('');
  } catch(e) {
    document.getElementById('offender-list').innerHTML =
      '<div class="empty-state"><div class="icon">⚠</div>' + e.message + '</div>';
  }
}

function escHtml(s) {
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

loadData();
</script>
</body>
</html>
"""

@app.route("/")
def index():
    return render_template_string(TEMPLATE, threshold=REPEAT_THRESHOLD)

@app.route("/api/data")
def api_data():
    if not Path(DATA_FILE).exists():
        return jsonify({
            "stats": {"total_hidden": 0, "total_scanned": 0, "last_run": None},
            "repeat_offenders": [],
            "high_risk": 0
        })

    with open(DATA_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)

    repeat_offenders = [
        info for info in data["offenders"].values()
        if info["count"] >= REPEAT_THRESHOLD
    ]
    repeat_offenders.sort(key=lambda x: -x["count"])

    high_risk = sum(1 for o in repeat_offenders if o["count"] >= 5)

    return jsonify({
        "stats": data.get("stats", {}),
        "repeat_offenders": repeat_offenders,
        "high_risk": high_risk
    })

if __name__ == "__main__":
    print("\n🚀 대시보드 시작: http://localhost:5000")
    print("   Ctrl+C로 종료\n")
    app.run(debug=False, port=5000)
