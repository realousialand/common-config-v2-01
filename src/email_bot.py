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
from bs4 import BeautifulSoup
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

def is_valid_academic_text(text):
    if not text or len(text) < 500: return False
    junk_triggers = ["access denied", "security check", "human verification", "cloudflare", "403 forbidden", "404 not found", "robot", "captcha", "please enable cookies"]
    head = text[:1000].lower()
    if any(t in head for t in junk_triggers): return False
    return True

def startup_check():
    logger.info("🔧 启动自检...")
    try:
        tag = chr(91) + "Image of Graph" + chr(93)
        test_str = "Test " + tag
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
                    if isinstance(content, list): 
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

# 🟢 V23.2 智能嗅探增强版
def sniff_real_pdf_link(initial_url, html_content):
    try:
        soup = BeautifulSoup(html_content, 'html.parser')
        
        # 1. Stork 专用 ID
        stork_btn = soup.find('a', id='full_text_available_anchor', href=True)
        if stork_btn: return stork_btn['href']

        # 2. 元数据
        meta_pdf = soup.find('meta', {'name': 'citation_pdf_url'})
        if meta_pdf and meta_pdf.get('content'): return meta_pdf['content']
            
        # 3. 广谱特征搜索 (Class/ID/Href/Text)
        for a in soup.find_all('a', href=True):
            href = a['href'].lower()
            
            # 获取标签内的所有文字（包括隐藏的、span里的）
            text = a.get_text(" ", strip=True).lower()
            
            # 获取 class 属性字符串
            classes = " ".join(a.get('class', [])).lower()
            
            # 判定条件：
            # A. 链接本身含有 .pdf 或 /article-pdf/ (期刊常用路径)
            is_pdf_link = '.pdf' in href or '/article-pdf/' in href
            
            # B. 上下文暗示这是下载按钮 (文字或样式包含 pdf/download)
            is_download_btn = 'pdf' in text or 'download' in text or 'full text' in text or 'pdf' in classes or 'download' in classes
            
            if is_pdf_link and is_download_btn:
                # 修复相对路径
                if href.startswith('/'):
                    parsed = urlparse(initial_url)
                    return f"{parsed.scheme}://{parsed.netloc}{a['href']}"
                return a['href']
                
    except Exception as e:
        logger.warning(f"    ⚠️ 嗅探失败: {e}")
    return None

def fetch_content(item):
    url = clean_google_url(item.get('url'))
    if not url:
        if item.get("type") == "doi": return fetch_abstract(item)
        return None, "No URL", None
    
    logger.info(f"    🔍 [下载] {url[:40]}...")
    try:
        r = requests.get(url, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}, timeout=30, stream=True, allow_redirects=True)
        
        if r.status_code == 429: return None, "Rate Limit", None
        
        final_url = r.url
        ct = r.headers.get('Content-Type', '').lower()
        
        if 'application/pdf' in ct or final_url.lower().endswith('.pdf'):
            fp = get_path(item['id'])
            with open(fp, "wb") as f:
                for chunk in r.iter_content(8192): f.write(chunk)
            if os.path.getsize(fp) < 2000:
                os.remove(fp)
                return None, "Too Small", None
            return pymupdf4llm.to_markdown(fp), "PDF", fp
            
        else:
            logger.info("    🕵️ 这是一个网页，尝试嗅探 PDF 链接...")
            html_text = r.text
            real_pdf_url = sniff_real_pdf_link(final_url, html_text)
            
            if real_pdf_url:
                logger.info(f"    🚀 嗅探成功，二次下载: {real_pdf_url[:40]}...")
                r2 = requests.get(real_pdf_url, headers={"User-Agent": "Mozilla/5.0"}, timeout=30, stream=True)
                if 'application/pdf' in r2.headers.get('Content-Type', '').lower():
                    fp = get_path(item['id'])
                    with open(fp, "wb") as f:
                        for chunk in r2.iter_content(8192): f.write(chunk)
                    if os.path.getsize(fp) > 2000:
                        return pymupdf4llm.to_markdown(fp), "PDF", fp
            
            logger.info("    ⚠️ 无法下载 PDF，转为摘要分析")
            if item.get("type") == "doi": return fetch_abstract(item)
            return None, "Not PDF", None

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
    if ctype == "ABSTRACT_ONLY":
        title_part = "Unknown"
        abstract_part = txt
        m = re.search(r"TITLE:\s*(.*?)\n\nABSTRACT:\s*(.*)", txt, re.DOTALL)
        if m:
            title_part = m.group(1).strip()
            abstract_part = m.group(2).strip()
            
        sys_prompt = "你是一个专业的学术翻译助手。"
        user_prompt = f"请将以下学术摘要翻译成通顺的中文（仅输出翻译内容，不要任何前缀）：\n\n{abstract_part}"
        try:
            res = client.chat.completions.create(
                model=LLM_MODEL_NAME, messages=[{"role": "system", "content": sys_prompt}, {"role": "user", "content": user_prompt}], temperature=0.3
            )
            trans = res.choices[0].message.content.strip()
            return title_part, f"**【摘要翻译】**\n\n{trans}"
        except:
            return title_part, f"摘要翻译失败。原文：\n{abstract_part[:500]}..."

    sys_prompt = "你是一名学术研究助手。请务必用【中文】回答。"
    user_prompt = f"""
    # 格式铁律
    第一行必须严格输出英文原标题，格式：TITLE: <English Title>
    
    # 拒答指令
    如果提供的 Content 是错误页面、无法访问、乱码或非学术论文，请直接回答 "INVALID_CONTENT"。
    
    # 任务：基于文献内容，用【中文】按以下板块深入分析。请使用 Markdown 列表和加粗突出重点：
    1. **基本信息**：标题、作者、期刊/会议（全称）、年份、关键词。
    2. **研究领域**：推断领域及影响力。
    3. **背景与缺口**：现状是什么？解决了什么具体缺口？
    4. **方法论**：关键技术、实验设计、理论框架、创新点。
    5. **结果与结论**：核心实证结果。
    6. **术语解释**：解释2-3个专业术语（面向非专业读者）。
    7. **贡献分析**：主要优势与贡献。
    8. **局限与未来**：样本量、假设限制等。
    9. **相关文献**：推荐3-5篇基础或后续研究。
    10. **搜索建议**：数据库搜索关键词。
    11. **链接信息**：提供DOI链接或官方链接。
    12. **量化细节**：（若为量化研究）列出数据/数据集、变量、模型、统计方法、数据来源、处理方法、结果。

    类型: {ctype}
    内容: 
    {txt[:45000]}
    """
    res = client.chat.completions.create(
        model=LLM_MODEL_NAME,
        messages=[{"role": "system", "content": sys_prompt}, {"role": "user", "content": user_prompt}],
        temperature=0.3
    )
    raw = res.choices[0].message.content.strip()
    
    if "INVALID_CONTENT" in raw:
        raise ValueError("LLM判断内容无效")

    clean = raw.replace("```markdown", "").replace("```", "").strip()
    
    title = "Unknown"
    body = clean
    m = re.search(r"TITLE:\s*(.*)", clean, re.I)
    if m:
        title = m.group(1).strip()
        body = clean.replace(m.group(0), "").strip()
    return title, body

def md_to_styled_html(md_text):
    html = markdown.markdown(md_text, extensions=['extra', 'nl2br'])
    html = re.sub(r'<h3>', '<h3 style="color:#2c3e50; border-bottom:2px solid #3498db; padding-bottom:8px; margin-top:20px;">', html)
    html = re.sub(r'<strong>', '<strong style="background-color:#fff3cd; padding:0 4px; border-radius:3px; color:#333;">', html)
    html = re.sub(r'<ul>', '<ul style="padding-left:20px; color:#444; line-height:1.6;">', html)
    html = re.sub(r'<li>', '<li style="margin-bottom:5px;">', html)
    html = re.sub(r'<p>', '<p style="margin:10px 0; line-height:1.6; color:#333;">', html)
    return html

def send_mail(subj, md_content, files=[]):
    styled_body = md_to_styled_html(md_content)
    full_html = f"""
    <!DOCTYPE html>
    <html>
    <body style="font-family: 'Helvetica Neue', Helvetica, Arial, sans-serif; background-color: #f4f6f9; padding: 20px; margin: 0;">
        <div style="max-width: 800px; margin: 0 auto; background: white; border-radius: 10px; box-shadow: 0 4px 15px rgba(0,0,0,0.05); overflow: hidden;">
            <div style="background: #2c3e50; color: white; padding: 20px; text-align: center;">
                <h2 style="margin: 0; font-size: 24px;">{subj}</h2>
                <p style="margin: 5px 0 0 0; opacity: 0.8; font-size: 14px;">{datetime.date.today()}</p>
            </div>
            <div style="padding: 30px;">{styled_body}</div>
            <div style="background: #f8f9fa; padding: 15px; text-align: center; border-top: 1px solid #eee; color: #888; font-size: 12px;">
                Generated by AI Research Assistant 🤖
            </div>
        </div>
    </body>
    </html>
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
        logger.info(f"✅ 邮件已发送: {subj}")
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
    first_sent = False

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
            badge = " (仅摘要)" if ctype == "ABSTRACT_ONLY" else ""
            
            card = f"""
### {disp} {badge}
> **{tt}**

{ans}
            """
            reports.append(card)
            db.update_status(pid, "ANALYZED", {"real_title": disp})

            if not first_sent:
                logger.info("🚀 首单即送...")
                att_list = [fp] if (item["status"]=="DOWNLOADED" and os.path.exists(fp)) else []
                send_mail(f"⚡ [预览] {disp}", card, att_list)
                first_sent = True

        except Exception as e:
            logger.error(f"分析失败: {e}")
            db.inc_retry(pid)
            db.update_status(pid, "ANALYSIS_FAILED")

    # 4. 发送
    if reports:
        if len(reports) == 1 and first_sent: pass
        else:
            zips = []
            cz, csz = [], 0
            for f in atts:
                s = os.path.getsize(f)
                if csz+s > MAX_EMAIL_ZIP_SIZE: zips.append(cz); cz, csz = [f], s
                else: cz.append(f); csz += s
            if cz: zips.append(cz)
            
            full_md = "\n\n---\n\n".join(reports)
            
            if not zips: send_mail(f"🤖 AI 日报 ({len(reports)})", full_md)
            else:
                for i, zf in enumerate(zips):
                    zn = f"p_{i+1}.zip"
                    with zipfile.ZipFile(zn, 'w', zipfile.ZIP_DEFLATED) as z:
                        for f in zf: z.write(f, os.path.basename(f))
                    send_mail(f"🤖 AI 日报 ({i+1})", full_md if i==0 else "附件", [zn])
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
