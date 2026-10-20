#!/usr/bin/env python3
"""요약 모델 후보 테스트: python3 model_test.py <모델1> <모델2> ...
각 모델로 샘플 소식 5건을 한국어 요약해 속도, 한국어 여부, 원문 근거 통과 여부를 잰다.
합격 기준: 한 건 평균 15초 이내(봇은 한 번에 최대 8건, 2분 주기) + 5건 중 4건 이상이 'AI 요약'으로 통과(나머지는 원문 발췌로 대체된 것).
전송, 저장 없음. 첫 호출은 모델을 메모리에 올리느라 느리므로 워밍업 1건을 시간에서 뺀다."""
import importlib.util
import os
import sys
import tempfile
import time

here = os.path.dirname(os.path.abspath(__file__))
os.environ.setdefault("AI_NEWS_BOT_DIR", tempfile.mkdtemp())
spec = importlib.util.spec_from_file_location("bot", os.path.join(here, "ai_news_bot.py"))
bot = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bot)

SAMPLES = [
    ("Claude", "신규 모델/업데이트", "Introducing Claude Haiku 5.5",
     "Claude Haiku 5.5 is our fastest and most affordable small model, built for high-volume and cost-sensitive tasks."),
    ("Gemini", "신규 모델/업데이트", "Gemini 4 Argon: our most capable model yet",
     "Gemini 4 Argon improves reasoning and coding, and is rolling out to the Gemini app and API starting today."),
    ("ChatGPT", "프로모션/한도", "Doubling Codex usage limits for a week",
     "We are doubling usage limits for Codex for all paid plans through Sunday to celebrate the new release."),
    ("Claude", "공식 새 글", "Claude Code release notes",
     "Added a new /resume command and fixed a bug where long sessions could freeze the terminal."),
    ("Gemini", "신규 모델/업데이트", "Gemma 4 now available",
     "Gemma 4 comes in three sizes and supports 140 languages. Weights are available for download today."),
]

models = sys.argv[1:]
if not models:
    sys.exit("쓰는 법: python3 model_test.py <모델1> <모델2>")
for m in models:
    bot.KO_MODEL = m
    bot.ko_summary(*SAMPLES[0])  # 워밍업(시간 제외)
    times, ok, outs = [], 0, []
    for company, label, title, snippet in SAMPLES:
        t = time.time()
        text, kind = bot.ko_summary(company, label, title, snippet)
        times.append(time.time() - t)
        ok += kind == "ai"
        outs.append((kind, text))
    avg = sum(times) / len(times)
    verdict = "합격" if avg <= 15 and ok >= 4 else "불합격"
    print(f"\n=== {m} : 평균 {avg:.1f}초, AI 요약 통과 {ok}/{len(SAMPLES)} → {verdict}")
    for (kind, text), (_, _, title, _) in zip(outs, SAMPLES):
        print(f"  [{kind or '없음'}] {title} => {text}")
