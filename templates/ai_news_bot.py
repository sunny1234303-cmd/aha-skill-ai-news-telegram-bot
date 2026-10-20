#!/usr/bin/env python3
"""AI 소식 텔레그램 봇 — Claude / Gemini / ChatGPT 신규 모델·프로모션(토큰 증정)·장애를 감시해 즉시 알림.

설계 원칙
- 놓치지 않기: 이름이 아닌 구조로 감지(모델 ID 목록 비교·변경이력 페이지), 전송 실패 시 '본 것'으로 기록하지 않고 재시도,
  소스 접속이 계속 실패하면 알림, 소스별 첫 수집 이후 새로 뜨는 건 전부 후보.
- 환각 방지: 장애·모델ID 알림은 LLM 없이 고정 템플릿. LLM 요약은 숫자·영문 고유명사가 원문에 없으면 폐기하고
  원문 발췌로 대체. 모든 알림에 원문 링크 포함, 요약에는 'AI·참고용' 표시.
- 무료 공개 피드만 사용(API 키·유료 호출 없음). 요약은 로컬 Ollama.
- 토큰/채팅ID: ~/.ai-news-bot/.env (저장소 밖). 값은 출력·로그에 남기지 않음.
- --dry 로 전송·상태저장 없이 확인.
"""
import gzip
import hashlib
import html
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

DIR = os.environ.get("AI_NEWS_BOT_DIR") or os.path.expanduser("~/.ai-news-bot")
ENV = os.path.join(DIR, ".env")
STATE = os.path.join(DIR, "state.json")
UA = "Mozilla/5.0 (ai-news-bot)"
DRY = "--dry" in sys.argv
SNIPPETS = {}  # 항목 key -> 원문 조각
META = {}      # 항목 key -> 부가정보(장애 상태/영향 등)
OLLAMA = "http://localhost:11434/api/generate"
KO_MODEL = os.environ.get("AI_NEWS_KO_MODEL", "qwen2.5:7b-instruct")  # 한국어 요약 모델(필수). model_test.py로 고른 모델 이름
MAX_ALERTS_PER_RUN = 8   # 한 번에 너무 많이 쏟아지면 나머지는 다음 실행으로
FAIL_ALERT_AFTER = 10    # 연속 실패 횟수(2분 간격 → 약 20분)

PROMO = re.compile(r"free|credit|bonus|promo|double|limit|reset|gift|giveaway|discount|무료|크레딧|증정|한도|리셋", re.I)
PROMO_STRONG = re.compile(r"free (?:credit|usage|tokens?|access|trial|month)|\bcredits?\b|bonus|giveaway|doubl(?:e|ed|ing)|2x (?:usage|limits?)|usage limits?|rate limits?|reset|무료|크레딧|증정|한도|리셋", re.I)
MODEL = re.compile(r"introduc|launch|releas|announc|new model|now available|generally available|preview|출시|공개", re.I)
# 'Gemini 4 Argon: ...' 처럼 출시 단어 없이 모델명+버전으로 시작하는 제목 (고객사례 'X with GPT-6'은 제외)
VERSIONED = re.compile(r"^(?:introducing\s+)?(?:gemini|gemma|gpt|claude|chatgpt|o\d|veo|imagen|nano banana|lyria|genie)[\w\s.\-]*\d", re.I)
CASE_STUDY = re.compile(r"\b(with|how|using|turns|scaling|cuts?|saves?|builds?|trusts?|reviews?|completes?)\b", re.I)

STATUS_KO = {"investigating": "조사 중", "identified": "원인 파악됨", "monitoring": "조치 후 모니터링 중",
             "resolved": "해결됨", "postmortem": "사후 분석 공개"}
IMPACT_KO = {"major": "큰 장애(major)", "critical": "심각한 장애(critical)"}

# (이름, 회사, 종류, URL)
FEEDS = [
    ("Anthropic 상태", "Claude", "status", "https://status.anthropic.com/api/v2/incidents.json"),
    ("OpenAI 상태", "ChatGPT", "status", "https://status.openai.com/api/v2/incidents.json"),
    ("Google Cloud 상태", "Gemini", "gcpstatus", "https://status.cloud.google.com/en/feed.atom"),
    ("Claude 모델 목록", "Claude", "model_ids:claude", "https://docs.claude.com/en/docs/about-claude/models/overview"),
    ("Gemini 모델 목록", "Gemini", "model_ids:gemini", "https://ai.google.dev/gemini-api/docs/models"),
    ("OpenAI 모델 목록", "ChatGPT", "model_ids:gpt", "https://platform.openai.com/docs/models"),
    ("Claude 릴리스 노트", "Claude", "changelog", "https://docs.claude.com/en/release-notes/overview"),
    ("Gemini API 변경 이력", "Gemini", "changelog", "https://ai.google.dev/gemini-api/docs/changelog"),
    ("Anthropic 뉴스", "Claude", "anthropic_news", "https://www.anthropic.com/news"),
    ("Claude Code 릴리스", "Claude", "feed_model", "https://github.com/anthropics/claude-code/releases.atom"),
    ("OpenAI 뉴스", "ChatGPT", "feed_news", "https://openai.com/news/rss.xml"),
    ("Google AI 블로그", "Gemini", "feed_news", "https://blog.google/technology/ai/rss/"),
    ("Gemini 앱 소식", "Gemini", "feed_news", "https://blog.google/products/gemini/rss/"),
    ("Gemini 모델 소식", "Gemini", "feed_news", "https://blog.google/innovation-and-ai/models-and-research/gemini-models/rss/"),
    ("Google DeepMind 블로그", "Gemini", "feed_news", "https://deepmind.google/blog/rss.xml"),
]

MONTHS = "January|February|March|April|May|June|July|August|September|October|November|December"


def load_env():
    env = {}
    if os.path.exists(ENV):
        for line in open(ENV, encoding="utf-8"):
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.rstrip("\n").split("=", 1)
                env[k.strip()] = v.strip()
    return env


def save_chat_id(chat_id):
    lines = open(ENV, encoding="utf-8").read().splitlines()
    lines = [l for l in lines if not l.startswith("TELEGRAM_CHAT_ID=")]
    lines.append(f"TELEGRAM_CHAT_ID={chat_id}")
    open(ENV, "w", encoding="utf-8").write("\n".join(lines) + "\n")


def http(url, data=None, timeout=20):
    req = urllib.request.Request(url, data=data, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read()
    if raw[:2] == b"\x1f\x8b":  # 일부 서버가 요청 없이도 gzip으로 응답
        raw = gzip.decompress(raw)
    return raw.decode("utf-8", "replace")


def tg(token, method, **params):
    data = urllib.parse.urlencode(params).encode()
    return json.loads(http(f"https://api.telegram.org/bot{token}/{method}", data))


def discover_chat_id(token):
    res = tg(token, "getUpdates")
    for u in reversed(res.get("result", [])):
        m = u.get("message") or {}
        if m.get("chat", {}).get("type") == "private":
            return m["chat"]["id"]
    return None


def strip(s):
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", s or ""))).strip()


def ns_findall(root, tag):
    return [e for e in root.iter() if e.tag.split("}")[-1] == tag]


def child_text(e, tag):
    for c in e:
        if c.tag.split("}")[-1] == tag:
            return (c.text or "").strip()
    return ""


def entries(xml_text):
    """RSS/Atom 공통 -> [(id, title, link)]"""
    root = ET.fromstring(xml_text)
    out = []
    for e in ns_findall(root, "item") + ns_findall(root, "entry"):
        link = child_text(e, "link")
        if not link:
            for c in e:
                if c.tag.split("}")[-1] == "link" and c.get("href"):
                    link = c.get("href")
                    break
        eid = child_text(e, "guid") or child_text(e, "id") or link
        SNIPPETS[eid] = strip(child_text(e, "description") or child_text(e, "summary") or child_text(e, "content"))[:700]
        out.append((eid, strip(child_text(e, "title")), link))
    return out


def fetch(kind, url):
    """-> [(key, title, link, flag)]  flag: outage / newmodel / official / model(피드)"""
    items = []
    if kind == "status":
        data = json.loads(http(url))
        for inc in data.get("incidents", [])[:15]:
            if inc.get("impact") not in ("major", "critical"):
                continue  # minor/none 장애는 알림 제외 (major 이상만)
            upd = (inc.get("incident_updates") or [{}])[0]
            key = f"{inc['id']}:{inc.get('status')}"
            META[key] = {"status": inc.get("status"), "impact": inc.get("impact")}
            SNIPPETS[key] = strip(upd.get("body"))[:400]
            items.append((key, f"{inc.get('name')} — {inc.get('status')}", inc.get("shortlink") or "", "outage"))
    elif kind == "gcpstatus":
        for eid, title, link in entries(http(url)):
            if re.search(r"vertex|gemini|generative|ai platform", title, re.I):
                items.append((eid, title, link, "outage"))
    elif kind.startswith("model_ids:"):
        # 이름과 상관없이 문서의 모델 ID 목록이 늘어나면 신규 모델로 간주 (키워드 의존 X)
        prefix = kind.split(":", 1)[1]
        text = http(url)
        ids = set(re.findall(rf'{prefix}-[a-z0-9.]+(?:-[a-z0-9.]+)*', text))
        for i in sorted(ids):
            i = re.sub(r'\.(png|webp|jpg|svg)$', '', i)
            if re.search(r'\d', i) and not re.search(r'texture|system-card|-api$|docs', i):
                items.append((i, f"새 모델 ID 감지: {i}", url, "newmodel"))
    elif kind == "changelog":
        # 날짜 제목(Month d, yyyy)으로 구간을 나누고, 구간 내용 해시로 '추가·수정'도 감지
        text = http(url)
        pat = rf'<h[23][^>]*>(?:\s*<[^>]+>)*\s*((?:{MONTHS}) \d{{1,2}}, 20\d\d)'
        marks = list(re.finditer(pat, text))
        for n, m in enumerate(marks[:6]):
            end = marks[n + 1].start() if n + 1 < len(marks) else min(len(text), m.start() + 20000)
            body = strip(text[m.start():end])
            date = m.group(1)
            body = body.replace(date, "", 1).strip()[:900]
            key = f"{date}:{hashlib.md5(body[:1500].encode()).hexdigest()[:8]}"
            SNIPPETS[key] = body
            items.append((key, f"변경 이력 · {date}", url, "official"))
    elif kind == "anthropic_news":
        page = http(url).replace('\\"', '"')
        # 모델 출시글은 /news/ 가 아니라 루트 경로(/claude-haiku-5-5)라서 featuredGridLink JSON에서 직접 추출
        for m in re.finditer(r'"date":"[\d-]+","subject":"[^"]*","summary":"([^"]*)","title":"([^"]+)","url":"([^"]+)"', page):
            summary, title, path = html.unescape(m.group(1)), html.unescape(m.group(2)), m.group(3)
            link = path if path.startswith("http") else "https://www.anthropic.com" + path
            SNIPPETS[path] = summary
            items.append((path, title, link, "official"))
        seen = []
        for m in re.finditer(r'href="(/news/[a-z0-9\-]+)"', page):
            if m.group(1) not in seen:
                seen.append(m.group(1))
        for path in seen[:20]:
            slug = path.rsplit("/", 1)[-1]
            items.append((path, slug.replace("-", " "), "https://www.anthropic.com" + path, "official"))
    else:  # feed_model / feed_news
        flag = "model"
        for eid, title, link in entries(http(url))[:25]:
            items.append((eid, title, link, flag))
    return items


def grounded(summary, source):
    """요약 속 숫자·영문 고유명사가 전부 원문에 있어야 통과 (없는 사실 지어내기 차단)"""
    src = source.lower()
    for tok in re.findall(r"[A-Za-z][A-Za-z0-9.\-]{2,}|\d[\d.,]*", summary):
        if tok.lower().strip(".,-") not in src:
            return False
    return True


JUNK = re.compile(r"key art|stylized|graphic|illustration|photo of|image of|thumbnail|alt text", re.I)


def first_sentences(text, limit=220):
    text = strip(text)
    if JUNK.search(text):  # 이미지 설명문이 요약 자리에 들어온 경우 버림
        return ""
    parts = re.split(r"(?<=[.!?])\s+", text)
    out = ""
    for p in parts:
        if len(out) + len(p) > limit and out:
            break
        out += (" " if out else "") + p
    return out[:limit + 40].strip()


def ko_summary(company, label, title, snippet):
    """로컬 Ollama로 한국어 요약. 반환 (텍스트, 종류) 종류: 'ai' | 'excerpt' | ''.
    환각 방지: 원문에 없는 숫자·영문 고유명사가 있으면 폐기하고 원문 발췌로 대체."""
    if JUNK.search(snippet or ""):
        snippet = ""
    source = f"{title} {snippet}"
    excerpt = first_sentences(snippet) if snippet else ""
    if not snippet and re.search(r"[가-힣]", title):
        return "", ""
    prompt = (
        "[원문]\n"
        f"회사: {company}\n분류: {label}\n제목: {title}\n내용: {snippet or '(제목만 있음)'}\n\n"
        "[지시]\n위 AI 서비스 소식을 반드시 한국어로 한 문장(길어도 두 문장)으로 요약해라. 말투는 친근하게 '~했어요', '~해요'로 끝내라. "
        "영어 문장으로 답하지 마라. 신규 모델이면 '모델명 : 바뀐 점' 형태로 써라. "
        "예) 'GPT-6 Astra : 분석 기능이 좋아졌어요.' "
        "원문에 없는 내용·숫자·이름은 절대 추가하지 말고, 모르면 쓰지 마라. 숫자·모델명·제품명은 원문 그대로 써라. 요약문만 출력해라.\n\n[한국어 요약]\n"
    )
    for attempt in range(3):  # 영어로 답하거나 근거 없는 내용이 섞이면 재시도
        try:
            body = json.dumps({"model": KO_MODEL, "prompt": prompt, "stream": False,
                               "options": {"temperature": 0.1 + 0.2 * attempt, "num_predict": 160}}).encode()
            req = urllib.request.Request(OLLAMA, data=body, headers={"Content-Type": "application/json"})
            out = json.loads(urllib.request.urlopen(req, timeout=60).read())["response"].strip()
        except Exception:
            break
        if re.search(r"[\u3040-\u30ff\u3400-\u9fff]", out):  # 중국어 한자, 일본어가 섞이면 폐기 (qwen 계열에서 실제로 발생)
            continue
        ko_chars = len(re.findall(r"[가-힣]", out))
        if out and ko_chars >= max(8, len(out) * 0.25) and grounded(out, source):
            return out[:300], "ai"
    return (excerpt, "excerpt") if excerpt else ("", "")


def ollama_ready():
    """요약 모델이 올라마에 설치돼 응답하는지 확인 (요약은 이 봇의 필수 기능)"""
    try:
        tags = json.loads(http(OLLAMA.replace("/api/generate", "/api/tags"), timeout=5))
        names = [m.get("name", "") for m in tags.get("models", [])]
        return KO_MODEL in names or f"{KO_MODEL}:latest" in names
    except Exception:
        return False


def classify(company, flag, title, snippet=""):
    """알림 대상이면 (태그, 라벨) 반환, 아니면 None. 태그: new/gift/alert/news"""
    if flag == "outage":
        return "alert", "장애/이슈"
    if flag == "newmodel":
        return "new", "신규 모델 (문서 목록 기준)"
    promo = PROMO.search(title) or PROMO_STRONG.search(snippet)
    model = MODEL.search(title)
    if promo:
        return "gift", "프로모션/한도"
    if model or (VERSIONED.search(title) and not CASE_STUDY.search(title)):
        return "new", "신규 모델/업데이트"
    if flag == "official":
        return "news", "공식 새 글"
    return None


def build_message(tag, label, company, src, title, link, key):
    head = f"[{tag}! {company.lower()}] {title}"
    body = ""
    if label.startswith("신규 모델 (문서"):
        body = "📝 공식 문서의 모델 목록에 새 모델 ID가 추가됐어요. 링크에서 확인하세요."
    elif tag == "alert":
        m = META.get(key, {})
        if m:
            body = f"📝 상태: {STATUS_KO.get(m.get('status'), m.get('status'))} · 영향도: {IMPACT_KO.get(m.get('impact'), m.get('impact'))}"
            orig = first_sentences(SNIPPETS.get(key, ""), 200)
            if orig:
                body += f"\n원문: {orig}"
    else:
        text, kind = ko_summary(company, label, title, SNIPPETS.get(key, ""))
        if kind == "ai":
            body = f"📝 {text}\n(AI 요약·참고용 — 정확한 내용은 링크 원문 확인)"
        elif kind == "excerpt":
            body = f"📄 원문 발췌: {text}"
    msg = head + (f"\n\n{body}" if body else "")
    return (msg + f"\n\n출처: {src}\n{link}").strip()


def main():
    os.makedirs(DIR, exist_ok=True)
    env = load_env()
    token = env.get("TELEGRAM_BOT_TOKEN", "")
    if not token:
        print("TELEGRAM_BOT_TOKEN 없음 — .env 확인")
        return 1
    chat_id = env.get("TELEGRAM_CHAT_ID", "")
    if not chat_id:
        cid = discover_chat_id(token)
        if not cid:
            print("chat_id 없음 — 텔레그램에서 새 봇에게 /start 를 먼저 보내세요")
            return 2
        save_chat_id(cid)
        chat_id = str(cid)
        print("chat_id 저장 완료")

    state = json.load(open(STATE)) if os.path.exists(STATE) else {}
    inited = state.setdefault("_init", {})
    fails = state.setdefault("_fail", {})
    if ollama_ready():
        fails["_ollama"] = 0
    else:  # 요약 모델이 안 돌아도 소식은 원문 발췌로 계속 보내고, 3회 연속(약 6분)이면 한 번 알림
        fails["_ollama"] = fails.get("_ollama", 0) + 1
        print(f"[warn] 요약 모델 '{KO_MODEL}' 응답 없음 (올라마 실행 여부, 모델 설치 확인)")
        if fails["_ollama"] == 3 and not DRY:
            try:
                tg(token, "sendMessage", chat_id=chat_id,
                   text=f"[alert! bot] 요약 모델 '{KO_MODEL}'이 응답하지 않아요. 올라마가 켜져 있는지, 모델이 설치돼 있는지 확인해 주세요. 그동안 소식은 요약 없이 원문 발췌로 가요.")
            except Exception:
                pass
    alerts = []
    for name, company, kind, url in FEEDS:
        try:
            items = fetch(kind, url)
            fails[name] = 0
        except Exception as ex:
            print(f"[warn] {name}: {type(ex).__name__}")
            fails[name] = fails.get(name, 0) + 1
            if fails[name] == FAIL_ALERT_AFTER and not DRY:
                try:
                    tg(token, "sendMessage", chat_id=chat_id,
                       text=f"[alert! bot] 감시 소스 점검 필요: '{name}' 접속이 계속 실패하고 있어요. 이 소스의 소식은 지금 놓칠 수 있어요.\n{url}")
                except Exception:
                    pass
            continue
        seen = set(state.get(name, []))
        was_inited = inited.get(name) or bool(seen)  # 기존 상태 파일과 호환
        fresh = [i for i in items if i[0] not in seen]
        inited[name] = True
        if not was_inited:  # 소스별 첫 성공 수집은 조용히 기록만 (알림 폭탄 방지)
            state[name] = list({i[0] for i in items})[-300:]
            continue
        state[name] = list(seen | {i[0] for i in items})[-300:]
        for key, title, link, flag in fresh:
            c = classify(company, flag, title, SNIPPETS.get(key, ""))
            if c:
                alerts.append((c[0], c[1], company, name, title, link, key))

    sent = 0
    for n, (tag, label, company, src, title, link, key) in enumerate(alerts):
        if n >= MAX_ALERTS_PER_RUN:  # 나머지는 '못 본 것'으로 되돌려 다음 실행에서 이어 보냄
            if key in state.get(src, []):
                state[src].remove(key)
            continue
        msg = build_message(tag, label, company, src, title, link, key)
        if DRY:
            print("DRY>", msg.replace("\n", " | "))
            continue
        try:
            tg(token, "sendMessage", chat_id=chat_id, text=msg, disable_web_page_preview="false")
            sent += 1
            time.sleep(0.5)
        except Exception as ex:  # 전송 실패 → 본 것으로 기록하지 않아 다음 실행에서 재시도
            print(f"[warn] 전송 실패 {src}: {type(ex).__name__}")
            if key in state.get(src, []):
                state[src].remove(key)
    if not DRY:
        json.dump(state, open(STATE, "w"), ensure_ascii=False)
        os.chmod(STATE, 0o600)
    if not DRY:  # 생존 신호: watchdog이 이 파일이 오래되면 봇 멈춤으로 경고
        open(os.path.join(DIR, "heartbeat"), "w").write(str(int(time.time())))
    print(f"done: alerts={len(alerts)} sent={sent}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
