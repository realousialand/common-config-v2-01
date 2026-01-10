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
from tenacity import retry, stop_after_attempt, wait_exponential, before_sleep_log

# --- 配置日志 ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# --- 全局变量 ---
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

TARGET_SUBJECTS = [
    "文献鸟", "Google Scholar Alert", "ArXiv", "Project MUSE", "new research", 
    "Stork", "ScienceDirect", "Chinese politics", "Imperial history", 
    "Causal inference", "new results", "The Accounting Review", 
    "recommendations available", "Table of Contents"
]

DATA_DIR = "data"
DB_FILE = os.path.join(DATA_DIR, "papers_database.json")
DOWNLOAD_DIR = "downloads"
MAX_EMAIL_ZIP_SIZE = 18 * 1024 * 1024 
socket.setdefaulttimeout(30)

client = OpenAI(api_key=LLM_API_KEY, base_url=LLM_BASE_URL)
cr = Crossref()
DOMAIN_LAST_ACCESSED = {}

# --- 辅助函数 ---
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

# --- 启动自检 (安全拼接版) ---
def startup_check():
    logger.info("🔧 执行启动自检...")
    try:
        # 🟢 修复：使用 ASCII 码拼接，避免字符串被系统截断
        # 构造 "[Image of Graph]"
        safe_tag = chr(91) + "Image of Graph" + chr(93)
        test_str = "Test " + safe_tag
        
        # 测试正则
        re.sub(r'\[Image of ([^\]]+)\]', 'IMG', test_str)
        
        # 测试 URL 清洗
        t_url = "https://www.google.com/url?q=https://arxiv.org/pdf/1.pdf"
        if "arxiv.org" not in clean_google_url(t_url):
            raise ValueError("URL清洗失败")
            
        logger.info("✅ 自检通过")
    except Exception as e:
        logger.critical(f"❌ 自检失败: {e}")
        exit(1)

# --- 数据库类 ---
class PaperDB:
    def __init__(self, filepath):
        self.filepath = filepath
        self.data = self._load()

    def _load(self):
        if os.path.exists(self.filepath):
            try:
                with open(self.filepath, 'r', encoding='utf-8') as f:
                    content = json.load(f)
                    # 自动修复 List -> Dict
                    if isinstance(content, list):
                        logger.warning("⚠️ 迁移数据库格式 List->Dict")
                        new_data = {}
                        for item in content:
                            if isinstance(item, dict) and 'id' in item:
                                new_data[item['id']] = item
                        return new_data
                    if isinstance(content, dict): return content
            except Exception as e:
                logger.error(f"加载数据库失败: {e}")
        return {}

    def save(self):
        try:
            os.makedirs(os.path.dirname(self.filepath), exist_ok=True)
            with open(self.filepath, 'w', encoding='utf-8') as f:
                json.dump(self.data, f, indent=2, ensure_ascii=False)
        except Exception as e:
            logger.error(f"保存数据库失败: {e}")

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
            if item["status"] == "NEW":
                res.append(item)
            elif item["status"] == "DOWNLOAD_FAILED" and item.get("retry", 0) < MAX_RETRIES:
                res.append(item)
        return res[:limit]

    def get_pending_analysis(self, limit=BATCH_SIZE):
        res = []
        if not isinstance(self.data, dict): return res
        for pid, item in self.data.items():
            if item["status"] in ["DOWNLOADED", "ABSTRACT_ONLY"]:
                res.append(item)
            elif item["status"] == "ANALYSIS_FAILED" and item.get("retry", 0) < MAX_RETRIES:
                res.append(item)
        return res[:limit]

    def inc_retry(self, pid):
        if pid in self.data:
            self.data[pid]["retry"] = self.data[pid].get("retry", 0) + 1
            self.save()

# --- 核心逻辑 ---

@retry(stop=stop_after_attempt(3), wait=wait_exponential(min=2, max=10), reraise=False)
def translate_title(text):
    if not text or len(text) < 5 or "Unknown" in text: return ""
    res = client.chat.completions.create(
        model=LLM_MODEL_NAME,
        messages=[{"role": "user", "content": f"Translate title to Chinese: {text}"}],
        temperature=0.1
    )
    return res.choices[0].message.content.strip()

def get_meta_safe(src):
    t = src.get('title', '')
    if t and "Unknown" not in t: return t
    if src.get('type') == 'arxiv': return f"ArXiv {src.get('id')}"
    return "Unknown Title"

def extract_titles(text):
    logger.info("    🧠 [智能提取] 分析邮件标题...")
    try:
        res = client.chat.completions.create(
            model=LLM_MODEL_NAME, 
            messages=[{"role": "user", "content": f"Extract academic titles as JSON list. Text: {text[:3000]}"}],
            temperature=0.1
        )
        return json.loads(res.choices[0].message.content.strip().replace("```json", "").replace("```", "").strip())
    except: return []

@retry(stop=stop_after_attempt(3), wait=wait_exponential(min=4, max=20))
def search_doi(title):
    logger.info(f"    🔍 [Crossref] 搜 DOI: {title[:30]}...")
    res = cr.works(query=title, limit=1)
    if res['message']['items']:
        it = res['message']['items'][0]
        return it.get('DOI'), it.get('title', [title])[0]
    return None, None

@retry(stop=stop_after_attempt(3), wait=wait_exponential(min=4, max=10))
def get_oa_link(doi):
    r = requests.get(f"https://api.unpaywall.org/v2/{doi}?email=bot@example.com", timeout=10)
    if r.status_code == 200:
        d = r.json()
        if d.get('is_oa') and d.get('best_oa_location'): return d['best_oa_location']['url_for_pdf']
    return None

def extract_body_urls(msg):
    text = ""
    urls = set()
    def grep_url(t): return [u.rstrip('.,;)]}') for u in re.findall(r'(https?://[^\s"\'<>]+)', t)]
    
    if msg.is_multipart():
        for p in msg.walk():
            if p.get_content_type() == "text/html":
                try:
                    payload = p.get_payload(decode=True).decode(errors='ignore')
                    urls.update(re.findall(r'href=["\']([^"\']+)["\']', payload, re.IGNORECASE))
                    text += re.sub('<[^<]+?>', ' ', payload) + "\n"
                except: pass
            elif p.get_content_type() == "text/plain":
                try:
                    payload = p.get_payload(decode=True).decode(errors='ignore')
                    text += payload + "\n"
                    urls.update(grep_url(payload))
                except: pass
    else:
        try:
            payload = msg.get_payload(decode=True).decode(errors='ignore')
            text += payload
            urls.update(grep_url(payload))
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
    return os.path.join(DOWNLOAD_DIR, f"{re.sub(r'[\\/*?]', '_', pid)}.pdf")

def fetch_content(item):
    url = clean_google_url(item.get('url'))
    if not url:
        if item.get("type") == "doi": return fetch_abstract(item)
        return None, "No URL", None
    logger.info(f"    🔍 [下载] {url[:50]}...")
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

@retry(stop=stop_after_attempt(3), wait=wait_exponential(
