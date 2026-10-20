#!/usr/bin/env python3
"""3개월 정기 운영점검 → 텔레그램 "[Bot, Check]" 알림.
  python3 model_checkup.py            # 매일 실행해도 됨: 마지막 점검 후 90일이 안 지났으면 아무것도 안 함
  python3 model_checkup.py --force    # 기한과 상관없이 지금 점검
  python3 model_checkup.py --dry      # 텔레그램 전송, 기록 저장 없이 메시지만 출력
  기본은 HTML 보고서를 사진으로 보냄(크롬/엣지/크로미움 필요, 없으면 글로 자동 대체).
  --text                              # 글로만 보내고 싶을 때 (환경변수 AI_NEWS_CHECKUP_FORMAT=text 도 같음)
  --dry 와 같이 쓰면 사진을 만들어 경로만 출력
점검 내용: ① 올라마 프로그램 버전 ② 쓰는 요약 모델이 새로 업데이트됐는지 ③ 올라마 라이브러리에 새로 올라온,
이 컴퓨터 메모리에 들어가는 모델 후보 ④ 봇 건강 상태(bot_health.py).
이상이 없으면 "검토를 완료했습니다. 계속 사용하셔도 됩니다."로 알린다.
새 후보는 '나왔다'는 안내일 뿐 더 좋은지는 model_test.py로 테스트해야 안다. 모델을 자동으로 받거나 바꾸지 않는다."""
import hashlib
import html as htmllib
import shutil
import importlib.util
import json
import os
import re
import subprocess
import sys
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
DAYS = 90
DRY = "--dry" in sys.argv
FORCE = "--force" in sys.argv or DRY
IMAGE = "--text" not in sys.argv and os.environ.get("AI_NEWS_CHECKUP_FORMAT") != "text"  # 기본은 사진, 브라우저가 없으면 글로 대체


def load_bot():
    for cand in (os.path.join(HERE, "ai_news_bot.py"),):
        spec = importlib.util.spec_from_file_location("bot", cand)
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
        return m
    raise SystemExit("ai_news_bot.py를 찾을 수 없음")


bot = load_bot()
DIR = bot.DIR
MODEL = bot.KO_MODEL
STATE = os.path.join(DIR, "checkup.json")
UA = {"User-Agent": "Mozilla/5.0 (ai-news-bot-checkup)"}


def get(url, accept=None, timeout=20):
    h = dict(UA)
    if accept:
        h["Accept"] = accept
    return urllib.request.urlopen(urllib.request.Request(url, headers=h), timeout=timeout).read()


def ver(s):
    return tuple(int(x) for x in re.findall(r"\d+", s)[:3])


def ram_gb():
    try:
        if sys.platform == "darwin":
            return int(subprocess.run(["sysctl", "-n", "hw.memsize"], capture_output=True, text=True).stdout) / 2**30
        if sys.platform.startswith("linux"):
            for line in open("/proc/meminfo"):
                if line.startswith("MemTotal"):
                    return int(line.split()[1]) / 2**20
        if sys.platform == "win32":
            out = subprocess.run(["powershell", "-NoProfile", "-Command",
                                  "(Get-CimInstance Win32_ComputerSystem).TotalPhysicalMemory"], capture_output=True, text=True).stdout
            return int(out.strip()) / 2**30
    except Exception:
        pass
    return None


state = json.load(open(STATE, encoding="utf-8")) if os.path.exists(STATE) else {}
if not FORCE and time.time() - state.get("last", 0) < DAYS * 86400:
    print("아직 점검 시기가 아님 (마지막 점검 후 90일 안 지남)")
    sys.exit(0)

problems, oks = [], []

# ① 올라마 프로그램
try:
    server = json.loads(get("http://localhost:11434/api/version", timeout=5))["version"]
    latest = json.loads(get("https://api.github.com/repos/ollama/ollama/releases/latest"))["tag_name"].lstrip("v")
    if ver(server) < ver(latest):
        problems.append(f"🔄 올라마 업데이트가 있어요: 설치 {server} → 최신 {latest} (앱의 메뉴에서 업데이트, 맥은 메뉴바 올라마 아이콘)")
    else:
        oks.append(f"🟢 올라마 {server} (최신)")
except Exception as ex:
    problems.append(f"❓ 올라마 버전을 확인하지 못했어요 ({type(ex).__name__}). 올라마가 켜져 있는지 확인해 주세요")

# ② 쓰는 요약 모델이 업데이트됐는지 (올라마 라이브러리 모델만 비교 가능)
try:
    tags = json.loads(get("http://localhost:11434/api/tags", timeout=5))["models"]
    local = next((m["digest"] for m in tags if m["name"] in (MODEL, MODEL + ":latest")), None)
    if local is None:
        problems.append(f"❌ 요약 모델 '{MODEL}'이 설치돼 있지 않아요")
    elif MODEL.startswith(("hf.co/", "huggingface.co/")):
        oks.append(f"🟡 요약 모델 {MODEL.split('/')[-1]} (허깅페이스 모델이라 자동 비교 불가, 필요하면 직접 확인)")
    else:
        repo, _, tag = MODEL.partition(":")
        remote = hashlib.sha256(get(f"https://registry.ollama.ai/v2/library/{repo}/manifests/{tag or 'latest'}")).hexdigest()
        if remote == local:
            oks.append(f"🟢 요약 모델 {MODEL} (최신 상태)")
        else:
            problems.append(f"🔄 요약 모델 {MODEL}이 새 버전으로 업데이트됐어요. 받으려면 ollama pull {MODEL} (받은 뒤 model_test.py로 품질 확인)")
except Exception as ex:
    problems.append(f"❓ 요약 모델 업데이트를 확인하지 못했어요 ({type(ex).__name__})")

# ③ 새로 올라온 모델 후보 (메모리에 들어가는 것만)
cands = []
try:
    page = get("https://ollama.com/library?sort=newest").decode("utf-8", "replace")
    found = {}
    for blk in re.split(r'<li\s+class="flex items-baseline', page)[1:]:
        m = re.search(r'href="/library/([^"]+)"', blk)
        if not m:
            continue
        pulls = re.search(r'<span >([\d.,]+)([KM]?)</span>\s*<span class="hidden sm:flex">&nbsp;Pulls', blk)
        n = float(pulls.group(1).replace(",", "")) * {"": 1, "K": 1e3, "M": 1e6}[pulls.group(2)] if pulls else 0
        desc = re.search(r'<p class="max-w-lg[^>]*>([^<]*)</p>', blk)
        caps = set(re.findall(r'text-\[13px\]">([a-z]+)</span>', blk))
        found[m.group(1)] = (n, (desc.group(1).strip() if desc else ""), caps)
    seen = set(state.get("library", []))
    if not seen:
        oks.append("📝 새 모델 비교 기준을 처음 기록했어요. 다음 점검부터 새로 올라온 모델을 알려 드려요")
    else:
        mem = ram_gb()
        budget = (mem * 0.35) if mem else 8  # 모델 파일이 메모리의 35% 안에 들어가는 것만
        new = sorted((k for k in found if k not in seen and "embedding" not in found[k][2]), key=lambda k: -found[k][0])[:12]
        for name in new:
            try:
                tp = get(f"https://ollama.com/library/{name}/tags").decode("utf-8", "replace")
            except Exception:
                continue
            fits = []
            for t, gb in re.findall(r'/library/(' + re.escape(name) + r':[\w.\-]+)"[^>]*>.*?([\d.]+)GB', tp, re.S)[:60]:
                if 1 <= float(gb) <= budget and "cloud" not in t and "-q" not in t:
                    fits.append((float(gb), t))
            if fits:
                gb, t = max(fits)
                cands.append((name, t, gb, found[name][1][:110]))
        cands = cands[:5]
    state["library"] = sorted(found)
except Exception as ex:
    problems.append(f"❓ 새 모델 목록을 확인하지 못했어요 ({type(ex).__name__})")

# ④ 봇 건강 상태
try:
    env = dict(os.environ, AI_NEWS_BOT_DIR=DIR, AI_NEWS_KO_MODEL=MODEL)
    out = subprocess.run([sys.executable, "-I", os.path.join(HERE, "bot_health.py")], capture_output=True, text=True, env=env, timeout=60).stdout
    bad = [l for l in out.splitlines() if l.startswith(("[WARN]", "[FAIL]"))]
    if bad:
        problems += ["🩺 봇 상태: " + l.split("] ", 1)[-1] for l in bad]
    else:
        oks.append("🟢 봇 상태 이상 없음")
except Exception as ex:
    problems.append(f"❓ 봇 상태 점검을 못 했어요 ({type(ex).__name__})")

# 메시지
today = time.strftime("%Y-%m-%d")
lines = [f"🔍 [Bot, Check] 운영점검(3개월) ({today})\n"]
if not problems and not cands:
    lines.append("✅ 검토를 완료했습니다. 계속 사용하셔도 됩니다.\n")
    lines += oks
    lines.append("🟢 새로 나온 소형 모델 후보 없음")
else:
    lines.append("⚠️ 확인이 필요한 것이 있어요\n")
    lines += problems
    if cands:
        lines.append(f"\n🆕 새로 올라온 모델 후보 (이 컴퓨터 메모리 기준, 현재 모델 {MODEL})")
        for name, tag, gb, desc in cands:
            lines.append(f"▫️ {tag} ({gb:g}GB)\n   {desc} (영문 원문)")
        lines.append("\n💡 후보는 '새로 나왔다'는 안내예요. 더 나은지는 테스트해야 알아요. Claude Code에서 \"AI 소식 봇 모델 점검해줘\"라고 하면 현재 모델과 비교 테스트를 해 드려요.")
    if oks:
        lines.append("\n✅ 이상 없는 것")
        lines += oks
msg = "\n".join(lines)
print(msg)


def find_browser():
    cands_ = ["/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
              "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
              "/Applications/Chromium.app/Contents/MacOS/Chromium",
              r"C:\Program Files\Google\Chrome\Application\chrome.exe",
              r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"]
    for c in cands_:
        if os.path.exists(c):
            return c
    for n in ("google-chrome", "chromium", "chromium-browser", "microsoft-edge"):
        if shutil.which(n):
            return shutil.which(n)
    return None


def render_png():
    """보고서를 HTML로 그려 사진(PNG)으로 저장. 브라우저가 없거나 실패하면 None"""
    br = find_browser()
    if not br:
        print("[warn] 크롬/엣지를 찾지 못해 사진 대신 글로 보냄")
        return None
    ok = not problems and not cands
    e = htmllib.escape
    W = 720  # 사진 폭 고정, 높이는 내용 길이에 맞춰 달라짐

    def split(t):  # "🔄 제목: 설명" -> (제목, 설명). 앞의 이모지는 떼고 사진에는 아이콘만 쓴다
        t = re.sub(r"^[^\w\[(]+\s*", "", t)
        head, sep, rest = t.partition(": ")
        return (head, rest) if sep else (t, "")
    items = [("warn",) + split(x) for x in problems]
    items += [("new", f"새 모델 후보 {tag} ({gb:g}GB)", desc) for _, tag, gb, desc in cands]
    items += [("ok",) + split(x) for x in oks]
    if ok:
        items.append(("ok", "새로 나온 소형 모델 후보가 없어요", ""))
    nxt = time.strftime("%Y-%m-%d", time.localtime(time.time() + DAYS * 86400))
    need = len(problems) + len(cands)
    color, bg = ("#15803d", "#dcf5e5") if ok else ("#b45309", "#fff1d9")
    svg = ('<svg width="46" height="46" viewBox="0 0 24 24" fill="none" stroke="%s" stroke-width="3" stroke-linecap="round" stroke-linejoin="round">%s</svg>'
           % (color, '<path d="M5 12.5l4.5 4.5L19 7.5"/>' if ok else '<path d="M12 6v8"/><circle cx="12" cy="18.2" r=".6" fill="%s"/>' % color))
    dots = {"warn": "#d97706", "new": "#2563eb", "ok": "#16a34a"}
    rows = "".join(
        f'<div class="r"><span class="dot" style="background:{dots[st]}"></span><div><b>{e(t)}</b>' + (f"<em>{e(d)}</em>" if d else "") + "</div></div>"
        for st, t, d in items)
    body = (f'<div class="tag">[Bot, Check] 운영점검(3개월)</div>'
            f'<div class="hero"><div class="disc" style="background:{bg}">{svg}</div><div>'
            f'<b style="color:{color}">{"검토를 완료했습니다" if ok else "확인이 필요한 것이 있어요"}</b>'
            f'<span>{"계속 사용하셔도 됩니다" if ok else f"확인 {need}건 · 이상 없음 {len(oks)}건"}</span></div></div>'
            f'<div class="list">{rows}</div>'
            f'<div class="foot"><span>{today} 점검</span><span>다음 점검 {nxt}</span></div>')
    page = f"""<!doctype html><meta charset="utf-8"><style>
body{{margin:0;width:{W}px;background:#fff}}
#c{{background:#fff;color:#16203a;font-family:-apple-system,'Apple SD Gothic Neo','Malgun Gothic',sans-serif;padding:40px 40px 34px}}
.tag{{font-size:17px;font-weight:600;color:#5a6479}}
.hero{{display:flex;align-items:center;gap:22px;margin:22px 0 26px}}
.disc{{flex:none;width:84px;height:84px;border-radius:50%;display:grid;place-items:center}}
.hero b{{display:block;font-size:34px;line-height:1.25;letter-spacing:-.02em}}
.hero span{{display:block;margin-top:6px;font-size:17px;color:#5a6479}}
.list{{border-top:2px solid #16203a}}
.r{{display:grid;grid-template-columns:14px 1fr;gap:0 16px;padding:18px 0;border-bottom:1px solid #e2e6ee}}
.dot{{width:14px;height:14px;border-radius:50%;margin-top:7px}}
.r b{{font-size:21px;line-height:1.35}}
.r em{{display:block;font-style:normal;font-size:17px;color:#5a6479;margin-top:3px;line-height:1.5}}
.foot{{margin-top:20px;font-size:15px;color:#7a8396;display:flex;justify-content:space-between}}
</style><div id="c">{body}</div>
<script>document.documentElement.setAttribute('data-h',document.getElementById('c').offsetHeight)</script>"""
    hp = os.path.join(DIR, "checkup-report.html")
    png = os.path.join(DIR, "checkup-report.png")
    open(hp, "w", encoding="utf-8").write(page)
    height = 190 + 62 * (len(problems) + len(oks) + 1) + 90 * len(cands) + (60 if cands else 0)  # 재지 못할 때의 어림값
    try:  # 1차: 실제 내용 높이를 재고, 2차: 그 높이로 캡처
        dom = subprocess.run([br, "--headless=new", "--disable-gpu", f"--window-size={W},1200", "--dump-dom", "file://" + hp],
                             capture_output=True, text=True, timeout=45).stdout
        mh = re.search(r'data-h="(\d+)"', dom)
        if mh:
            height = int(mh.group(1))
    except Exception:
        pass
    try:
        if os.path.exists(png):
            os.remove(png)
        subprocess.run([br, "--headless=new", "--disable-gpu", "--hide-scrollbars", f"--window-size={W},{height}",
                        f"--screenshot={png}", "file://" + hp], capture_output=True, timeout=45)
    except Exception:
        return None
    return png if os.path.exists(png) and os.path.getsize(png) > 1000 else None


def send_photo(token, chat_id, path, caption):
    boundary = "----aibot" + str(int(time.time()))
    parts = []
    for k, v in (("chat_id", chat_id), ("caption", caption)):
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode())
    parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="photo"; filename="report.png"\r\nContent-Type: image/png\r\n\r\n'.encode()
                 + open(path, "rb").read() + b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode())
    req = urllib.request.Request(f"https://api.telegram.org/bot{token}/sendPhoto", data=b"".join(parts),
                                 headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    try:
        return json.loads(urllib.request.urlopen(req, timeout=30).read()).get("ok", False)
    except Exception:
        return False


if DRY:
    if IMAGE:
        print("사진 보고서:", render_png() or "만들지 못함")
    sys.exit(0)

env = bot.load_env()
sent_photo = False
if IMAGE:
    png = render_png()
    if png:
        head = "✅ 검토를 완료했습니다. 계속 사용하셔도 됩니다." if not problems and not cands else "⚠️ 확인이 필요한 것이 있어요"  # 내용은 사진 안에 있어서 한 줄만
        sent_photo = send_photo(env["TELEGRAM_BOT_TOKEN"], env["TELEGRAM_CHAT_ID"], png, head)
if not sent_photo:  # 사진을 못 만들거나 보내지 못하면 글로 대체
    bot.tg(env["TELEGRAM_BOT_TOKEN"], "sendMessage", chat_id=env["TELEGRAM_CHAT_ID"], text=msg, disable_web_page_preview="true")
state["last"] = int(time.time())
json.dump(state, open(STATE, "w", encoding="utf-8"), ensure_ascii=False)
os.chmod(STATE, 0o600)
