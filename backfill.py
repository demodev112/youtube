#!/usr/bin/env python3
"""
1회성: 과거 실행(예: 10/21 400개 일괄 검토)에서 숨겨진 댓글을
현재 기준(명백/심각/경미)으로 다시 판정해서 차단 대상 작성자를 찾는다.

- 기본은 미리보기(dry-run): 아무것도 바꾸지 않고 보고서만 출력
- --apply 를 주면 실제로 차단하고, 판정 결과를 누적 데이터(offender_data.json)에 합침

사용 예:
  python backfill.py --source backfill_src/offender_data.json            # 미리보기
  python backfill.py --source backfill_src/offender_data.json --apply    # 실제 차단

차단 규칙은 modbot.py 와 동일:
  - "명백" 이 2차 검수에서 확정되면 1회로 차단
  - "심각"(AI 지적만, 명백 기각 포함) 누적이 BAN_THRESHOLD_SEVERE 회 이상이면 차단 (기존 누적 횟수 포함)
  - "경미" 는 차단 근거가 아님
"""

import argparse
import json
from collections import Counter, defaultdict
from datetime import datetime

from googleapiclient.errors import HttpError

from modbot import (
    AI_BATCH_SIZE,
    AI_MODEL,
    AUTO_BAN,
    BAN_THRESHOLD_SEVERE,
    BLATANT_INSTANT_BAN,
    NEVER_HIDE_IDS,
    VIDEO_SPECIFIC_RULES,
    ai_judge_batch,
    ai_verify_blatant,
    classify_level,
    get_anthropic_client,
    get_authenticated_service,
    get_my_channel_id,
    get_video_info,
    load_data,
    log,
    reject_and_ban_author,
    save_data,
)

REPORT_FILE = "backfill_report.txt"
report_lines: list[str] = []


def out(line: str = ""):
    """로그와 보고서 파일에 같이 남김."""
    log.info(line)
    report_lines.append(line)


def chunks(seq, n):
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def text_of(snippet: dict) -> str:
    return snippet.get("textOriginal") or snippet.get("textDisplay", "")


def fetch_by_ids(youtube, ids: list[str]) -> dict[str, dict]:
    """댓글 ID 로 직접 조회 (50개씩). 채널 주인 권한이면 moderationStatus 도 함께 옴."""
    found = {}
    for chunk in chunks(ids, 50):
        try:
            resp = youtube.comments().list(
                part="snippet", id=",".join(chunk), textFormat="plainText"
            ).execute()
        except HttpError as e:
            log.warning(f"ID 조회 실패 ({len(chunk)}개): {e}")
            continue
        for item in resp.get("items", []):
            found[item["id"]] = item
    return found


def list_held_threads(youtube, channel_id: str, max_threads: int) -> list[dict]:
    threads, token = [], None
    while True:
        params = {
            # replies 를 같이 받으면 숨겨진 대댓글이 스레드에 딸려 옴 (comments.list 로는 안 나옴)
            "part": "snippet,replies",
            "allThreadsRelatedToChannelId": channel_id,
            "maxResults": 100,
            "moderationStatus": "heldForReview",
            "textFormat": "plainText",
        }
        if token:
            params["pageToken"] = token
        try:
            resp = youtube.commentThreads().list(**params).execute()
        except HttpError as e:
            log.warning(f"검토 대기 스레드 조회 실패: {e}")
            break
        threads.extend(resp.get("items", []))
        token = resp.get("nextPageToken")
        if not token or len(threads) >= max_threads:
            break
    return threads


def judge_all(ai, comments: list[dict], titles: dict) -> tuple[dict, int, int]:
    """현재 기준으로 재판정. 반환: ({comment_id: 판정}, AI 호출 수, 판정 실패 댓글 수)."""
    groups: dict[str, list[dict]] = defaultdict(list)
    for c in comments:
        key = c["video_id"] if c["video_id"] in VIDEO_SPECIFIC_RULES else ""
        groups[key].append(c)

    judged, ai_calls, failed = {}, 0, 0
    for key in sorted(groups, key=lambda k: (k != "", k)):
        extra = VIDEO_SPECIFIC_RULES[key]["rules"] if key else ""
        for batch in chunks(groups[key], AI_BATCH_SIZE):
            ai_input, idmap = [], {}
            for i, c in enumerate(batch, 1):
                ai_input.append({
                    "id": i,
                    "label": "[대댓글]" if c["is_reply"] else "[댓글]",
                    "text": c["text"],
                    "video_title": titles.get(c["video_id"], {}).get("title", ""),
                    "parent_text": c["parent_text"],
                    "parent_is_operator": c.get("parent_is_operator", False),
                    "prev_replies": [],
                })
                idmap[i] = c
            tag = f" [전용 규칙: {key}]" if key else ""
            log.info(f"재판정 중... {len(batch)}개{tag}")
            results = ai_judge_batch(ai, ai_input, extra_rules=extra)
            ai_calls += 1
            if not results:
                log.error(f"판정 실패 배치 {len(batch)}개 — 이 댓글들은 차단 판단에서 제외")
                failed += len(batch)
                continue

            in_map = {d["id"]: d for d in ai_input}
            cands = [
                {**in_map[r["id"]], "first_reason": r.get("reason", "")}
                for r in results
                if r.get("action") == "HIDE"
                and r.get("severity") == "명백"
                and r.get("target") in ("운영자", "영상", "제품")
                and r.get("id") in in_map
            ]
            verdicts = {}
            if cands and AUTO_BAN and BLATANT_INSTANT_BAN:
                log.info(f"명백 후보 {len(cands)}개 2차 검수 중...")
                verdicts = ai_verify_blatant(ai, cands, extra_rules=extra)
                ai_calls += 1

            for r in results:
                c = idmap.get(r.get("id"))
                if not c:
                    continue
                verdict, vreason = verdicts.get(r["id"], ("", ""))
                judged[c["cid"]] = {
                    **c,
                    "level": classify_level(r, verdict),
                    "target": r.get("target", ""),
                    "reason": r.get("reason", ""),
                    "verify": vreason,
                }
    return judged, ai_calls, failed


def main():
    parser = argparse.ArgumentParser(description="과거 숨김 댓글 재판정·차단 (1회성)")
    parser.add_argument("--source", default="", help="원본 실행의 offender_data.json 경로 (그 실행에서 판정한 댓글만 대상)")
    parser.add_argument("--all-held", action="store_true", help="원본 실행과 상관없이 지금 숨김 상태인 댓글 전부 재판정")
    parser.add_argument("--skip-authors", default="", help="차단하지 않고 숨김만 유지할 작성자 표시 이름. | 로 구분")
    parser.add_argument("--apply", action="store_true", help="실제로 차단 (없으면 미리보기)")
    parser.add_argument("--max-held", type=int, default=1000, help="훑을 검토 대기 스레드 최대 수")
    args = parser.parse_args()
    if not args.source and not args.all_held:
        raise SystemExit("--source 또는 --all-held 중 하나는 필요합니다")
    skip_authors = {s.strip() for s in args.skip_authors.split("|") if s.strip()}

    mode = "실제 차단" if args.apply else "미리보기 (아무것도 바꾸지 않음)"
    scope = "지금 숨김 상태인 댓글 전부" if args.all_held else "원본 실행에서 판정했던 댓글"
    out("=" * 60)
    out(f"과거 숨김 댓글 재판정 — {mode} / 범위: {scope}")
    out(f"AI 모델: {AI_MODEL}")
    if skip_authors:
        out(f"차단 제외(숨김만 유지) 작성자 {len(skip_authors)}명")

    src_set: set[str] = set()
    if args.source:
        with open(args.source, encoding="utf-8") as f:
            src = json.load(f)
        src_set = set(src.get("processed", []))
        out(f"원본 실행에서 판정했던 댓글: {len(src_set)}개")

    youtube = get_authenticated_service()
    ai = get_anthropic_client()
    my_channel = get_my_channel_id(youtube)

    # 1) 검토 대기 스레드 목록: 최상위 댓글 + 스레드에 딸려 온 대댓글
    threads = list_held_threads(youtube, my_channel, args.max_held)
    found: dict[str, dict] = {}
    for t in threads:
        vid = t["snippet"].get("videoId", "")
        top = t["snippet"]["topLevelComment"]
        top["snippet"].setdefault("videoId", vid)
        found[top["id"]] = top
        for r in t.get("replies", {}).get("comments", []):
            r["snippet"].setdefault("videoId", vid)
            found[r["id"]] = r
    n_reply = sum(1 for it in found.values() if it["snippet"].get("parentId"))
    out(f"검토 대기 스레드 {len(threads)}개 / 최상위 {len(found) - n_reply}개, 딸려 온 대댓글 {n_reply}개")

    # 2) 이 목록에는 "대댓글만 숨겨진 스레드의 공개 최상위 댓글", "숨김 스레드의 공개 대댓글"도 섞여 나옴.
    #    공개 댓글은 ID 조회에 돌아오고 숨겨진 댓글은 돌아오지 않으므로, 돌아온 것은 제외
    visible = fetch_by_ids(youtube, list(found))
    held = {cid: it for cid, it in found.items() if cid not in visible}
    out(f"그중 공개 상태 {len(found) - len(held)}개 제외 → 숨김 상태 {len(held)}개")
    if not args.all_held:
        held = {cid: it for cid, it in held.items() if cid in src_set}
        out(f"원본 실행에서 판정했던 댓글로 좁힘 → {len(held)}개")

    # 제외: 예외 목록, 운영자 본인 댓글
    excluded = 0
    for cid in list(held):
        author = held[cid]["snippet"].get("authorChannelId", {}).get("value", "")
        if cid in NEVER_HIDE_IDS or author == my_channel:
            del held[cid]
            excluded += 1
    out(f"재판정 대상: {len(held)}개 (예외·본인 {excluded}개 제외)")

    if not held:
        out("재판정할 댓글이 없습니다.")
        _write_report()
        return

    # 대댓글의 원댓글 본문
    parent_ids = sorted({it["snippet"].get("parentId") for it in held.values() if it["snippet"].get("parentId")})
    parents = {pid: found[pid] for pid in parent_ids if pid in found}
    missing = [pid for pid in parent_ids if pid not in parents]
    if missing:
        parents.update(fetch_by_ids(youtube, missing))

    comments = []
    for cid, it in held.items():
        sn = it["snippet"]
        pid = sn.get("parentId")
        comments.append({
            "cid": cid,
            "author_name": sn.get("authorDisplayName", "알 수 없음"),
            "author_cid": sn.get("authorChannelId", {}).get("value", "unknown"),
            "text": text_of(sn),
            "video_id": sn.get("videoId", ""),
            "is_reply": bool(pid),
            "parent_text": text_of(parents[pid]["snippet"]) if pid and pid in parents else "",
            "parent_is_operator": bool(pid and pid in parents) and
                parents[pid]["snippet"].get("authorChannelId", {}).get("value", "") == my_channel,
        })
    titles = get_video_info(youtube, [c["video_id"] for c in comments])

    # 3) 현재 기준으로 재판정
    judged, ai_calls, failed = judge_all(ai, comments, titles)
    level_counts = Counter(j["level"] for j in judged.values())
    out(f"재판정 완료: {len(judged)}개 (AI 호출 {ai_calls}회, 판정 실패 {failed}개) / {dict(level_counts)}")

    # 4) 작성자별 집계 + 차단 계획 (기존 누적 횟수 포함)
    data = load_data()
    by_author: dict[str, list[dict]] = defaultdict(list)
    for j in judged.values():
        by_author[j["author_cid"]].append(j)

    plan = []
    for acid, items in by_author.items():
        prev = data["offenders"].get(acid, {})
        blatant = [j for j in items if j["level"] == "명백"]
        severe = [j for j in items if j["level"] in ("명백", "심각", "심각(명백 기각)")]
        prev_severe = prev.get("severe_count", 0)
        total_severe = prev_severe + len(severe)
        ban_comment, ban_reason = None, None
        skipped = items[0]["author_name"] in skip_authors
        if skipped:
            pass
        elif AUTO_BAN and acid != "unknown" and not prev.get("banned", False):
            if blatant and BLATANT_INSTANT_BAN:
                ban_comment, ban_reason = blatant[0], "명백 1회"
            elif severe and total_severe >= BAN_THRESHOLD_SEVERE:
                ban_comment, ban_reason = severe[-1], f"심각 {total_severe}회"
        plan.append({
            "acid": acid,
            "name": items[0]["author_name"],
            "items": items,
            "severe": severe,
            "prev_severe": prev_severe,
            "total_severe": total_severe,
            "already_banned": prev.get("banned", False),
            "skipped": skipped,
            "ban_comment": ban_comment,
            "ban_reason": ban_reason,
        })

    to_ban = [p for p in plan if p["ban_comment"]]
    to_ban.sort(key=lambda p: (p["ban_reason"] != "명백 1회", -p["total_severe"]))
    out("")
    out("=" * 60)
    out(f"■ 차단 대상: {len(to_ban)}명")
    for p in to_ban:
        bc = p["ban_comment"]
        out(f"  🚫 {p['name']} — {p['ban_reason']} (이번 심각 {len(p['severe'])} + 기존 {p['prev_severe']})")
        for j in p["items"]:
            mark = "→" if j["cid"] == bc["cid"] else " "
            out(f"     {mark} [{j['level']}] \"{j['text'][:80]}\"")
            out(f"         사유: {j['reason']}" + (f" / 2차: {j['verify']}" if j["verify"] else ""))

    skipped_plan = [p for p in plan if p["skipped"]]
    if skipped_plan:
        out("")
        out(f"■ 운영자 지정으로 차단 제외 (숨김만 유지): {len(skipped_plan)}명")
        for p in skipped_plan:
            for j in p["items"]:
                out(f"     {p['name']} [{j['level']}] \"{j['text'][:80]}\"")

    already = [p for p in plan if p["already_banned"]]
    if already:
        out("")
        out(f"■ 이미 차단된 사람: {len(already)}명 ({', '.join(p['name'] for p in already)})")

    watch = [p for p in plan if not p["ban_comment"] and p["severe"] and not p["already_banned"] and not p["skipped"]]
    out("")
    out(f"■ 심각 1회라 아직 차단 아님 (다음에 심각 1회 더면 차단): {len(watch)}명")
    for p in watch:
        for j in p["severe"]:
            out(f"     {p['name']} [{j['level']}] \"{j['text'][:80]}\"")

    mild = [j for j in judged.values() if j["level"] == "경미"]
    out("")
    out(f"■ 경미 (숨김만, 차단 근거 아님): {len(mild)}개")
    for j in mild:
        out(f"     {j['author_name']} \"{j['text'][:80]}\"")

    ok_now = [j for j in judged.values() if j["level"] == "OK"]
    out("")
    out(f"■ 현재 기준으로는 숨길 대상이 아님 (지금도 숨김 상태, 복구 검토용): {len(ok_now)}개")
    for j in ok_now:
        out(f"     {j['author_name']} \"{j['text'][:80]}\" — {j['reason']}")

    if not args.apply:
        out("")
        out("미리보기라서 아무것도 차단하지 않았고 누적 데이터도 바꾸지 않았습니다.")
        _write_report()
        return

    # 5) 실제 차단 + 누적 데이터 합치기
    out("")
    out("=" * 60)
    banned = 0
    now = datetime.now().isoformat()
    for p in plan:
        if p["skipped"]:
            continue  # 운영자가 "숨김만" 지정한 사람은 누적 횟수에도 넣지 않음
        offender = data["offenders"].setdefault(p["acid"], {
            "name": p["name"],
            "channel_id": p["acid"],
            "count": 0,
            "last_comment": "",
            "last_reason": "",
            "timestamps": [],
            "severe_count": 0,
            "banned": False,
            "banned_at": None,
            "channel_url": f"https://www.youtube.com/channel/{p['acid']}",
        })
        hidden_items = [j for j in p["items"] if j["level"] != "OK"]
        if p["ban_comment"]:
            bc = p["ban_comment"]
            try:
                reject_and_ban_author(youtube, bc["cid"])
                offender["banned"] = True
                offender["banned_at"] = now
                offender["ban_reason"] = f"{p['ban_reason']} (과거 일괄 재판정)"
                offender["ban_comment"] = bc["text"][:200]
                banned += 1
                out(f"🚫 차단 완료: {p['name']} ({p['ban_reason']})")
            except HttpError as e:
                out(f"차단 실패: {p['name']} | {e}")
        offender["name"] = p["name"]
        offender["count"] = offender.get("count", 0) + len(hidden_items)
        offender["severe_count"] = offender.get("severe_count", 0) + len(p["severe"])
        if hidden_items:
            offender["last_comment"] = hidden_items[-1]["text"][:100]
            offender["last_reason"] = f"[과거 재판정][{hidden_items[-1]['level']}] {hidden_items[-1]['reason']}"
            offender.setdefault("timestamps", []).extend([now] * len(hidden_items))

    processed = set(data["processed"])
    data["processed"].extend(cid for cid in judged if cid not in processed)
    data["stats"]["total_banned"] = data["stats"].get("total_banned", 0) + banned
    save_data(data)
    out(f"차단 {banned}/{len(to_ban)}명 완료. 판정 결과를 누적 데이터에 합쳤습니다.")
    _write_report()


def _write_report():
    with open(REPORT_FILE, "w", encoding="utf-8") as f:
        f.write("\n".join(report_lines) + "\n")


if __name__ == "__main__":
    main()
