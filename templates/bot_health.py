#!/usr/bin/env python3
"""봇 건강 점검: python3 bot_health.py   (읽기 전용, 전송 없음, .env는 열지 않음)
확인: 봇 생존 신호, 소스별 접속 실패, 요약 모델 설치/응답, 로그 오류, 상태 파일 크기, 올라마 버전.
환경변수: AI_NEWS_BOT_DIR(기본 ~/.ai-news-bot), AI_NEWS_KO_MODEL(기본 qwen2.5:7b-instruct)"""
import json
import os
import re
import subprocess
import time
import urllib.request

DIR = os.environ.get("AI_NEWS_BOT_DIR") or os.path.expanduser("~/.ai-news-bot")
MODEL = os.environ.get("AI_NEWS_KO_MODEL", "qwen2.5:7b-instruct")
rows = []


def current_feeds():
    """봇 스크립트의 FEEDS 이름 목록 (지운 소스의 옛 기록을 세지 않기 위해). 못 읽으면 None"""
    try:
        import importlib.util
        here = os.path.dirname(os.path.abspath(__file__))
        for cand in (os.path.join(DIR, "ai_news_bot.py"), os.path.join(here, "ai_news_bot.py")):
            if os.path.exists(cand):
                spec = importlib.util.spec_from_file_location("bot", cand)
                m = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(m)
                return {f[0] for f in m.FEEDS}
    except Exception:
        pass
    return None


def say(level, msg):
    rows.append(level)
    print(f"[{level}] {msg}")


# 1) 생존 신호 (봇이 매 실행 끝에 기록)
hb = os.path.join(DIR, "heartbeat")
if os.path.exists(hb):
    age = (time.time() - int(open(hb).read().strip() or 0)) / 60
    say("OK" if age <= 10 else "FAIL", f"마지막 실행 {age:.0f}분 전 (2분 주기라 10분 넘으면 멈춘 것)")
else:
    say("FAIL", "heartbeat 파일 없음: 봇이 한 번도 끝까지 돌지 못했거나 폴더가 다름")

# 2) 소스별 연속 실패 (state.json의 _fail)
st = os.path.join(DIR, "state.json")
if os.path.exists(st):
    state = json.load(open(st, encoding="utf-8"))
    fails = state.get("_fail", {})
    live = current_feeds()
    if live is not None:
        fails = {k: v for k, v in fails.items() if k.startswith("_") or k in live}
        stale = [k for k in state.get("_fail", {}) if not k.startswith("_") and k not in live]
        if stale:
            say("INFO", f"예전에 지운 소스의 기록이 남아 있음(무시함): {', '.join(stale)}")
    bad = {k: v for k, v in fails.items() if v and not k.startswith("_")}
    if bad:
        for k, v in bad.items():
            say("WARN" if v < 10 else "FAIL", f"소스 '{k}' 연속 {v}회 접속 실패: 주소가 바뀌었거나 막혔을 수 있음")
    else:
        say("OK", f"감시 소스 {len([k for k in fails if not k.startswith('_')])}곳 모두 최근 접속 성공")
    if fails.get("_ollama"):
        say("WARN", f"요약 모델 응답 없음이 {fails['_ollama']}회 연속")
    sizes = {k: len(v) for k, v in state.items() if isinstance(v, list)}
    if sizes:
        say("OK", f"본 소식 기록 {sum(sizes.values())}건 (소스당 최대 300건 유지)")
else:
    say("WARN", "state.json 없음: 첫 실행 전이거나 폴더가 다름")

# 3) 요약 모델
try:
    tags = json.loads(urllib.request.urlopen("http://localhost:11434/api/tags", timeout=5).read())
    names = [m["name"] for m in tags.get("models", [])]
    if MODEL in names or f"{MODEL}:latest" in names:
        say("OK", f"요약 모델 '{MODEL}' 설치됨. 설치된 다른 모델: {', '.join(n for n in names if n != MODEL) or '없음'}")
    else:
        say("FAIL", f"요약 모델 '{MODEL}'이 올라마에 없음 (ollama pull 필요)")
except Exception:
    say("FAIL", "올라마가 응답하지 않음 (앱 실행 또는 ollama serve 확인)")
try:
    r = subprocess.run(["ollama", "--version"], capture_output=True, text=True, timeout=5)
    out = r.stdout + r.stderr
    server = re.search(r"ollama version is ([\d.]+)", out)
    client = re.search(r"client version is ([\d.]+)", out)
    if client and server and client.group(1) != server.group(1):
        say("WARN", f"올라마 서버 {server.group(1)}와 명령줄 {client.group(1)} 버전이 다름: 앱과 명령줄을 같은 버전으로 맞추면 모델 받기와 실행 오류를 줄일 수 있음")
    else:
        say("OK", f"올라마 버전: {server.group(1) if server else '확인 불가'}")
except Exception:
    say("WARN", "ollama 명령을 찾을 수 없음")

# 4) 로그의 최근 오류
for name in ("err.log", "out.log"):
    path = os.path.join(DIR, name)
    if os.path.exists(path):
        tail = "\n".join(open(path, encoding="utf-8", errors="replace").read().splitlines()[-60:])
        errs = re.findall(r"Traceback|\[warn\][^\n]*", tail)
        if errs:
            now_bad = any(r in ("WARN", "FAIL") for r in rows)
            say("WARN" if (now_bad or "Traceback" in tail) else "INFO",
                f"{name} 최근 60줄에 오류/경고 {len(errs)}건" + ("" if now_bad else " (지금은 접속 정상이라 일시적인 끊김)") + ": " + "; ".join(sorted(set(errs))[:3]))
        else:
            say("OK", f"{name} 최근 오류 없음")

print("\n요약:", "문제 없음" if all(r in ("OK", "INFO") for r in rows) else f"FAIL {rows.count('FAIL')}건, WARN {rows.count('WARN')}건")
