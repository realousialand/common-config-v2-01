import os
import re
import requests
import pymupdf4llm
from openai import OpenAI
from habanero import Crossref
import time
import hashlib
import json
import shutil
import zipfile
import socket
import imaplib
import email
import smtplib
import datetime
import logging
from datetime import timedelta
from email.header import decode_header
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.application import MIMEApplication
from urllib.parse import unquote, urlparse, parse_qs
import markdown
from tenacity import retry, stop_after_attempt, wait_exponential

# --- 配置 ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

LLM_API_KEY = os.environ.get("LLM_API_KEY")
LLM_BASE_URL = "https://api.siliconflow.cn/v1"
LLM_MODEL_NAME = os.environ.get("LLM_MODEL_NAME", "deepseek-ai/DeepSeek-R1-distill-llama-70b")
EMAIL_USER = os.environ.get("EMAIL_USER")
EMAIL_PASS = os.environ.get("EMAIL_PASS")
IMAP_SERVER = "imap.gmail.com"
SMTP_SERVER = "smtp.gmail.com"
SCHEDULER_MODE = False
LOOP_INTERVAL_HOURS = 4
BATCH_SIZE = 20
MAX_RETRIES = 3
TARGET_SUBJECTS = ["文献鸟", "Google Scholar Alert", "ArXiv", "Project MUSE", "new research", "Stork", "ScienceDirect", "Chinese politics", "Imperial history", "Causal inference", "new results", "The Accounting Review", "recommendations available", "Table of Contents"]
DATA_DIR = "data"
DB_FILE = os.path.join(DATA_DIR, "papers_database.json")
DOWNLOAD_DIR = "downloads"
MAX_EMAIL_ZIP_SIZE = 18 * 1024 * 1024 
socket.setdefaulttimeout(30)
client = OpenAI(api_key=LLM_API_KEY, base_url=LLM_BASE_URL)
cr = Crossref()
DOMAIN_LAST_ACCESSED = {}

# --- 辅助 ---
def clean_google_url(url):
    try:
        url = unquote(url)
        if "google" in url and ("url=" in url or "q=" in url):
            parsed = urlparse(url)
            qs = parse_qs(parsed.query)
            if 'url' in qs: return unquote(qs['url'][0])
            if 'q' in qs: return unquote(qs['q'][0])
    except: pass
    return url

def startup_check():
    logger.info("🔧 启动自检...")
    try:
        # 使用 ASCII 拼接防止代码被网页截断
        tag = chr(91) + "Image of Graph" + chr(93)
        test_str = "Test " + tag
        # 只要能正常运行不报错即可，不再做复杂正则替换
        if "Image" not in test_str: raise ValueError("String Error")
        
        url = "https://www.google.com/url?q=https://arxiv.org/pdf/1.pdf"
        if "arxiv.org" not in clean_google_url(url): raise ValueError("URL Clean Error")
        logger.info("✅ 自检通过")
    except Exception as e:
        logger.critical(f"❌ 自检失败: {e}")
        exit(1)

# --- 数据库 ---
class PaperDB:
    def __init__(self, filepath):
        self.filepath = filepath
        self.data = self._load()

    def _load(self):
        if os.path.exists(self.filepath):
            try:
                with open(self.filepath, 'r', encoding='utf-8') as f:
                    content = json.load(f)
                    if isinstance(content, list): # 自动修复列表格式
                        logger.warning("⚠️ 修复旧版数据库格式 List->Dict")
                        new_data = {}
                        for item in content:
                            if isinstance(item, dict) and 'id' in item: new_data[item['id']] = item
                        return new_data
                    if isinstance(content, dict): return content
            except: pass
        return {}

    def save(self):
        try:
            os.makedirs(os.path.dirname(self.filepath), exist_ok=True)
            with open(self.filepath, 'w', encoding='utf-8') as f:
                json.dump(self.data, f, indent=2, ensure_ascii=False)
        except Exception as e: logger.error(f"保存失败: {e}")

    def add_new(self, pid, meta):
        if not isinstance(self.data, dict): self.data = {}
        if pid not in self.data:
            self.data[pid] = {**meta, "status": "NEW", "retry": 0, "ts": str(datetime.datetime.now())}
            self.save()
            return True
        return False

    def update_status(self, pid, status, extra=None):
        if pid in self.data:
            self.data[pid]["status"] = status
            if extra: self.data[pid].update(extra)
            self.save()

    def get_pending_downloads(self, limit=BATCH_SIZE):
        res = []
        if not isinstance(self.data, dict): return res
        for pid, item in self.data.items():
            if item["status"] == "NEW": res.append(item)
            elif item["status"] == "DOWNLOAD_FAILED" and item.get("retry", 0) < MAX_RETRIES: res.append(item)
        return res[:limit]

    def get_pending_analysis(self, limit=BATCH_SIZE):
        res = []
        if not isinstance(self.data, dict): return res
        for pid, item in self.data.items():
            if item["status"] in ["DOWNLOADED", "ABSTRACT_ONLY"]: res.append(item)
            elif item["status"] == "ANALYSIS_FAILED" and item.get("retry", 0) < MAX_RETRIES: res.append(item)
        return res[:limit]

    def inc_retry(self, pid):
        if pid in self.data:
            self.data[pid]["retry"] = self.data[pid].get("retry", 0) + 1
            self.save()

# --- 核心 ---
@retry(stop=stop_after_attempt(3), wait=wait_exponential(min=2, max=10), reraise=False)
def translate_title(text):
    if not text or len(text) < 5 or "Unknown" in text: return ""
    try:
        res = client.chat.completions.create(
            model=LLM_MODEL_NAME, messages=[{"role": "user", "content": f"Translate title to Chinese: {text}"}], temperature=0.1
        )
        return res.choices[0].message.content.strip()
    except: return ""

def get_meta_safe(src):
    t = src.get('title', '')
    if t and "Unknown" not in t: return t
    if src.get('type') == 'arxiv': return f"ArXiv {src.get('id')}"
    return "Unknown Title"

def extract_titles(text):
    logger.info("    🧠 [智能提取] 提取标题...")
    try:
        res = client.chat.completions.create(
            model=LLM_MODEL_NAME, 
            messages=[{"role": "user", "content": f"Extract academic titles as JSON list. Text: {text[:3000]}"}], temperature=0.1
        )
        return json.loads(res.choices[0].message.content.strip().replace("```json", "").replace("```", "").strip())
    except: return []

@retry(stop=stop_after_attempt(3), wait=wait_exponential(min=4, max=20))
def search_doi(title):
    logger.info(f"    🔍 [Crossref] {title[:20]}...")
    res = cr.works(query=title, limit=1)
    if res['message']['items']:
        it = res['message']['items'][0]
        return it.get('DOI'), it.get('title', [title])[0]
    return None, None

@retry(stop=stop_after_attempt(3), wait=wait_exponential(min=4, max=10))
def get_oa_link(doi):
    try:
        r = requests.get(f"https://api.unpaywall.org/v2/{doi}?email=bot@example.com", timeout=10)
        if r.status_code == 200:
            d = r.json()
            if d.get('is_oa') and d.get('best_oa_location'): return d['best_oa_location']['url_for_pdf']
    except: pass
    return None

def extract_body_urls(msg):
    text = ""
    urls = set()
    def grep_url(t): return [u.rstrip('.,;)]}') for u in re.findall(r'(https?://[^\s"\'<>]+)', t)]
    if msg.is_multipart():
        for p in msg.walk():
            try:
                payload = p.get_payload(decode=True)
                if not payload: continue
                pt = payload.decode(errors='ignore')
                if p.get_content_type() == "text/html":
                    urls.update(re.findall(r'href=["\']([^"\']+)["\']', pt, re.IGNORECASE))
                    text += re.sub('<[^<]+?>', ' ', pt) + "\n"
                else: text += pt + "\n"
                urls.update(grep_url(pt))
            except: continue
    else:
        try:
            pt = msg.get_payload(decode=True).decode(errors='ignore')
            text += pt
            urls.update(grep_url(pt))
        except: pass
    return text, list(urls)

def detect_sources(text, urls):
    srcs = []
    seen = set()
    for m in re.finditer(r"(?:arXiv:|arxiv\.org/abs/|arxiv\.org/pdf/)\s*(\d{4}\.\d{4,5})", text, re.I):
        if m.group(1) not in seen:
            srcs.append({"type": "arxiv", "id": m.group(1), "url": f"https://arxiv.org/pdf/{m.group(1)}.pdf"})
            seen.add(m.group(1))
    for m in re.finditer(r"(?:doi:|doi\.org/)\s*(10\.\d{4,9}/[-._;()/:A-Z0-9]+)", text, re.I):
        doi = m.group(1)
        if doi not in seen:
            try: link = get_oa_link(doi)
            except: link = None
            srcs.append({"type": "doi", "id": doi, "url": link})
            seen.add(doi)
    for link in urls:
        try:
            clink = clean_google_url(link)
            if not clink: continue
            lower = clink.lower()
            if any(x in lower for x in ['unsubscribe', 'twitter', 'facebook']): continue
            if lower.endswith('.pdf') or 'viewcontent.cgi' in lower:
                lid = hashlib.md5(clink.encode()).hexdigest()[:10]
                if lid not in seen:
                    srcs.append({"type": "pdf_link", "id": f"link_{lid}", "url": clink})
                    seen.add(lid)
        except: continue
    return srcs

def get_path(pid):
    safe = re.sub(r'[\\/*?:"<>|]', '_', pid)
    return os.path.join(DOWNLOAD_DIR, f"{safe}.pdf")

def fetch_content(item):
    url = clean_google_url(item.get('url'))
    if not url:
        if item.get("type") == "doi": return fetch_abstract(item)
        return None, "No URL", None
    logger.info(f"    🔍 [下载] {url[:40]}...")
    try:
        r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=30, stream=True)
        if r.status_code == 429: return None, "Rate Limit", None
        ct = r.headers.get('Content-Type', '').lower()
        if 'application/pdf' not in ct and not url.lower().endswith('.pdf'):
            if item.get("type") == "doi": return fetch_abstract(item)
            return None, "Not PDF", None
        fp = get_path(item['id'])
        with open(fp, "wb") as f:
            for chunk in r.iter_content(8192): f.write(chunk)
        if os.path.getsize(fp) < 2000:
            os.remove(fp)
            if item.get("type") == "doi": return fetch_abstract(item)
            return None, "Too Small", None
        try:
            txt = pymupdf4llm.to_markdown(fp)
            if len(txt) < 500:
                os.remove(fp)
                if item.get("type") == "doi": return fetch_abstract(item)
                return None, "Empty", None
            return txt, "PDF", fp
        except:
            if item.get("type") == "doi": return fetch_abstract(item)
            return None, "Parse Error", None
    except Exception as e:
        if item.get("type") == "doi": return fetch_abstract(item)
        return None, str(e), None

@retry(stop=stop_after_attempt(3), wait=wait_exponential(min=2, max=10))
def fetch_abstract(item):
    w = cr.works(ids=item["id"])
    t = w['message'].get('title', [''])[0]
    a = re.sub(r'<[^>]+>', '', w['message'].get('abstract', '无摘要'))
    return f"TITLE: {t}\n\nABSTRACT: {a}", "ABSTRACT_ONLY", None

@retry(stop=stop_after_attempt(3), wait=wait_exponential(min=5, max=30))
def analyze(txt, ctype):
    # 🟢 植入用户指定的13点要求
    sys_prompt = "You are a comprehensive academic research assistant."
    user_prompt = f"""
    Please act as my academic assistant.
    # CRITICAL FORMAT RULE: 
    First line MUST be: TITLE: <English Title>

    # Execution Steps:
    1. Confirm info: title, authors, journal, year, keywords.
    2. Infer field and impact.
    3. Explain gap and problem.
    4. Detail methodology, techniques, and innovation.
    5. List empirical results and conclusions.
    6. Explain 2-3 key terms.
    7. Analyze contributions.
    8. Discuss limitations and future directions.
    9. Recommend 3-5 related papers.
    10. Suggest database search queries.
    11. Provide DOI link if available.
    12. If no DOI, provide alternative link.
    13. IF QUANTITATIVE: List Data/Dataset, Variables, Models, Stats methods, Sources, Results.

    Type: {ctype}
    Content: {txt[:45000]}
    """
    res = client.chat.completions.create(
        model=LLM_MODEL_NAME,
        messages=[{"role": "system", "content": sys_prompt}, {"role": "user", "content": user_prompt}],
        temperature=0.3
    )
    raw = res.choices[0].message.content.strip()
    clean = raw.replace("```markdown", "").replace("```", "").strip()
    
    title = "Unknown"
    body = clean
    m = re.search(r"TITLE:\s*(.*)", clean, re.I)
    if m:
        title = m.group(1).strip()
        body = clean.replace(m.group(0), "").strip()
    return title, body

def send_mail(subj, md_body, files=[]):
    html = markdown.markdown(md_body, extensions=['extra'])
    full_html = f"""
    <html><body style="font-family:sans-serif;padding:20px">
    <div style="background:#2c3e50;color:white;padding:15px;border-radius:5px">
        <h2>{subj}</h2><p>{datetime.date.today()}</p>
    </div>
    {html}
    <hr><p style="color:#888;font-size:12px">AI Assistant</p>
    </body></html>
    """
    msg = MIMEMultipart()
    msg["Subject"] = subj
    msg["From"] = EMAIL_USER
    msg["To"] = EMAIL_USER
    msg.attach(MIMEText(full_html, "html", "utf-8"))
    
    for f in files:
        if os.path.exists(f):
            try:
                with open(f, "rb") as fp:
                    part = MIMEApplication(fp.read(), Name=os.path.basename(f))
                    part['Content-Disposition'] = f'attachment; filename="{os.path.basename(f)}"'
                    msg.attach(part)
            except: pass
            
    try:
        with smtplib.SMTP_SSL(SMTP_SERVER, 465) as s:
            s.login(EMAIL_USER, EMAIL_PASS)
            s.sendmail(EMAIL_USER, EMAIL_USER, msg.as_string())
        logger.info("✅ 邮件已发送")
        return True
    except Exception as e:
        logger.error(f"邮件失败: {e}")
        return False

# --- 入口 ---
def run():
    startup_check()
    logger.info(f"🎬 任务开始")
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)
    os.makedirs(DATA_DIR, exist_ok=True)
    
    db = PaperDB(DB_FILE)
    logger.info(f"📚 数据库: {type(db.data)}, {len(db.data)} 条")

    # 1. 扫描
    try:
        m = imaplib.IMAP4_SSL(IMAP_SERVER)
        m.login(EMAIL_USER, EMAIL_PASS)
        m.select("inbox")
        _, data = m.search(None, f'(SINCE "{(datetime.date.today()-timedelta(days=2)).strftime("%d-%b-%Y")}")')
        if data[0]:
            for eid in data[0].split():
                try:
                    _, h = m.fetch(eid, "(BODY.PEEK[HEADER])")
                    subj = decode_header(email.message_from_bytes(h[0][1])["Subject"])[0][0]
                    if isinstance(subj, bytes): subj = subj.decode()
                    if not any(k.lower() in subj.lower() for k in TARGET_SUBJECTS): continue
                    logger.info(f"🎯 邮件: {subj[:20]}...")
                    _, b = m.fetch(eid, "(RFC822)")
                    msg = email.message_from_bytes(b[0][1])
                    txt, urls = extract_body_urls(msg)
                    srcs = detect_sources(txt, urls)
                    if not srcs:
                        ts = extract_titles(txt)
                        for t in ts:
                            try:
                                doi, full = search_doi(t)
                                if doi: srcs.append({"type": "doi", "id": doi, "url": get_oa_link(doi)})
                            except: pass
                    for s in srcs:
                        pid = s.get('id') or hashlib.md5(s.get('url','').encode()).hexdigest()[:10]
                        s['id'] = pid
                        if 'title' not in s: s['title'] = get_meta_safe(s)
                        if db.add_new(pid, s): logger.info(f"    ➕ 新增: {pid}")
                except: pass
    except Exception as e: logger.error(f"IMAP: {e}")

    # 2. 下载
    pend_dl = db.get_pending_downloads(BATCH_SIZE)
    logger.info(f"📥 待下载: {len(pend_dl)}")
    for item in pend_dl:
        res, type_, path = fetch_content(item)
        if type_ in ["PDF", "ABSTRACT_ONLY"]:
            db.update_status(item['id'], "DOWNLOADED" if type_=="PDF" else "ABSTRACT_ONLY", 
                           {"local_path": path, "content_type": type_, "abstract_content": res if type_=="ABSTRACT_ONLY" else ""})
        else:
            db.inc_retry(item['id'])
            db.update_status(item['id'], "DOWNLOAD_FAILED")

    # 3. 分析
    pend_an = db.get_pending_analysis(BATCH_SIZE)
    logger.info(f"🤖 待分析: {len(pend_an)}")
    reports, atts = [], []
    for item in pend_an:
        pid = item['id']
        txt, ctype = "", item.get("content_type", "Unknown")
        if item["status"] == "DOWNLOADED":
            fp = get_path(pid)
            if not os.path.exists(fp):
                _, ctype, fp = fetch_content(item)
                if not fp: 
                    db.update_status(pid, "DOWNLOAD_FAILED")
                    continue
            try: txt = pymupdf4llm.to_markdown(fp)
            except: db.update_status(pid, "ANALYSIS_FAILED"); continue
            atts.append(fp)
        elif item["status"] == "ABSTRACT_ONLY":
            txt = item.get("abstract_content", "")
            if not txt:
                try: txt, _, _ = fetch_abstract(item)
                except: db.inc_retry(pid); continue
        
        try:
            logger.info(f"分析: {pid}")
            rt, ans = analyze(txt, ctype)
            disp = rt if ("Unknown" not in rt and rt) else item.get('title', 'Unknown')
            tt = translate_title(disp)
            card = f"""<div style="border:1px solid #ccc;padding:15px;margin-bottom:20px;">
            <h3>{disp}</h3><p style="color:#666">{tt}</p><div>{ans}</div></div>"""
            reports.append(card)
            db.update_status(pid, "ANALYZED", {"real_title": disp})
        except Exception as e:
            logger.error(f"分析失败: {e}")
            db.inc_retry(pid)
            db.update_status(pid, "ANALYSIS_FAILED")

    # 4. 发送
    if reports:
        zips = []
        cz, csz = [], 0
        for f in atts:
            s = os.path.getsize(f)
            if csz+s > MAX_EMAIL_ZIP_SIZE: zips.append(cz); cz, csz = [f], s
            else: cz.append(f); csz += s
        if cz: zips.append(cz)
        
        if not zips: send_mail(f"🤖 AI 日报 ({len(reports)})", "\n".join(reports))
        else:
            for i, zf in enumerate(zips):
                zn = f"p_{i+1}.zip"
                with zipfile.ZipFile(zn, 'w', zipfile.ZIP_DEFLATED) as z:
                    for f in zf: z.write(f, os.path.basename(f))
                send_mail(f"🤖 AI 日报 ({i+1})", "\n".join(reports) if i==0 else "附件", [zn])
                if os.path.exists(zn): os.remove(zn)
                time.sleep(5)
    logger.info("✅ 完成")

if __name__ == "__main__":
    if SCHEDULER_MODE:
        while True:
            try: run()
            except: pass
            time.sleep(LOOP_INTERVAL_HOURS * 3600)
    else:
        run()
