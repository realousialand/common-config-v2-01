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

# 🟢 引入 tenacity 重试库
from tenacity import retry, stop_after_attempt, wait_exponential, before_sleep_log

# --- 🛠️ 日志配置 ---
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)

# --- 🛠️ 全局配置区 ---
LLM_API_KEY = os.environ.get("LLM_API_KEY")
LLM_BASE_URL = "https://api.siliconflow.cn/v1"
LLM_MODEL_NAME = os.environ.get("LLM_MODEL_NAME", "deepseek-ai/DeepSeek-R1-distill-llama-70b")

EMAIL_USER = os.environ.get("EMAIL_USER")
EMAIL_PASS = os.environ.get("EMAIL_PASS")
IMAP_SERVER = "imap.gmail.com"
SMTP_SERVER = "smtp.gmail.com"

# 🟢 修复：全局定义调度变量，防止 NameError
SCHEDULER_MODE = False
LOOP_INTERVAL_HOURS = 4

BATCH_SIZE = 20
MAX_RETRIES = 3

TARGET_SUBJECTS = [
    "文献鸟", "Google Scholar Alert", "ArXiv", "Project MUSE", 
    "new research", "Stork", "ScienceDirect", "Chinese politics", 
    "Imperial history", "Causal inference", "new results", "The Accounting Review",
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

# --- 📚 数据库管理类 ---
class PaperDB:
    def __init__(self, filepath):
        self.filepath = filepath
        self.data = self._load()

    def _load(self):
        if os.path.exists(self.filepath):
            try:
                with open(self.filepath, 'r', encoding='utf-8') as f:
                    return json.load(f)
            except Exception as e:
                logger.error(f"加载数据库失败: {e}")
        return {}

    def save(self):
        try:
            os.makedirs(os.path.dirname(self.filepath), exist_ok=True)
            with open(self.filepath, 'w', encoding='utf-8') as f:
                json.dump(self.data, f, indent=2, ensure_ascii=False)
        except Exception as e:
            logger.critical(f"保存数据库失败: {e}")

    def add_new(self, pid, metadata):
        if pid not in self.data:
            self.data[pid] = {
                **metadata,
                "status": "NEW",
                "retry_count": 0,
                "created_at": str(datetime.datetime.now()),
                "history": []
            }
            self.save()
            return True
        return False

    def update_status(self, pid, status, extra_data=None):
        if pid in self.data:
            self.data[pid]["status"] = status
            self.data[pid]["updated_at"] = str(datetime.datetime.now())
            if extra_data:
                self.data[pid].update(extra_data)
            self.save()

    def get_pending_downloads(self, limit=BATCH_SIZE):
        candidates = []
        for pid, item in self.data.items():
            if item["status"] == "NEW":
                candidates.append(item)
            elif item["status"] == "DOWNLOAD_FAILED" and item.get("retry_count", 0) < MAX_RETRIES:
                candidates.append(item)
        return candidates[:limit]

    def get_pending_analysis(self, limit=BATCH_SIZE):
        candidates = []
        for pid, item in self.data.items():
            if item["status"] in ["DOWNLOADED", "ABSTRACT_ONLY"]:
                candidates.append(item)
            elif item["status"] == "ANALYSIS_FAILED" and item.get("retry_count", 0) < MAX_RETRIES:
                candidates.append(item)
        return candidates[:limit]

    def increment_retry(self, pid):
        if pid in self.data:
            self.data[pid]["retry_count"] = self.data[pid].get("retry_count", 0) + 1
            self.save()

# --- 🧠 核心功能 (带重试机制) ---

@retry(
    stop=stop_after_attempt(3), 
    wait=wait_exponential(multiplier=1, min=2, max=10),
    reraise=False
)
def translate_title(text):
    if not text or len(text) < 5 or "Unknown" in text: return ""
    try:
        completion = client.chat.completions.create(
            model=LLM_MODEL_NAME,
            messages=[{"role": "user", "content": f"请将以下学术论文标题翻译成中文（仅输出翻译后的文本）：{text}"}],
            temperature=0.1
        )
        return completion.choices[0].message.content.strip()
    except Exception as e:
        logger.warning(f"标题翻译失败: {e}")
        return ""

def get_metadata_safe(source_data):
    title = source_data.get('title', '')
    if title and "Unknown" not in title: return title
    s_id = source_data.get('id', '')
    if source_data.get('type') == 'arxiv': return f"ArXiv Paper {s_id}"
    return title or "Unknown Title"

def extract_titles_from_text(text):
    logger.info("    🧠 [智能提取] 正在分析邮件正文提取标题...")
    prompt = f"Extract academic paper titles from the text below. Return ONLY a JSON list of strings. Text: {text[:3000]}"
    try:
        completion = client.chat.completions.create(
            model=LLM_MODEL_NAME, messages=[{"role": "user", "content": prompt}], temperature=0.1
        )
        content = completion.choices[0].message.content.strip().replace("```json", "").replace("```", "").strip()
        return json.loads(content)
    except Exception as e:
        logger.warning(f"标题提取失败: {e}")
        return []

@retry(
    stop=stop_after_attempt(3), 
    wait=wait_exponential(multiplier=1, min=4, max=20),
    before_sleep=before_sleep_log(logger, logging.WARNING)
)
def search_doi_by_title(title):
    logger.info(f"    🔍 [Crossref] 搜索 DOI: {title[:30]}...")
    try:
        res = cr.works(query=title, limit=1)
        if res['message']['items']:
            item = res['message']['items'][0]
            return item.get('DOI'), item.get('title', [title])[0]
    except Exception:
        raise
    return None, None

@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=4, max=10))
def get_oa_link(doi):
    try:
        r = requests.get(f"https://api.unpaywall.org/v2/{doi}?email=bot@example.com", timeout=10)
        if r.status_code == 200:
            data = r.json()
            if data.get('is_oa') and data.get('best_oa_location'):
                return data['best_oa_location']['url_for_pdf']
    except Exception:
        raise
    return None

def clean_google_url(url):
    try:
        url = unquote(url)
        if "google" in url and ("url=" in url or "q=" in url):
            parsed = urlparse(url)
            qs = parse_qs(parsed.query)
            if 'url' in qs: return unquote(qs['url'][0])
            if 'q' in qs: return unquote(qs['q'][0])
    except Exception as e:
        logger.debug(f"URL 清洗失败: {e}")
    return url

def extract_body(msg):
    text = ""
    urls = set()
    def find_urls(t): return [u.rstrip('.,;)]}') for u in re.findall(r'(https?://[^\s"\'<>]+)', t)]
    
    if msg.is_multipart():
        for part in msg.walk():
            try:
                payload = part.get_payload(decode=True)
                if not payload: continue
                pt = payload.decode(errors='ignore')
                if "attachment" not in str(part.get("Content-Disposition")):
                    if part.get_content_type() == "text/html":
                        urls.update(re.findall(r'href=["\']([^"\']+)["\']', pt, re.IGNORECASE))
                        text += re.sub('<[^<]+?>', ' ', pt) + "\n"
                    else: text += pt + "\n"
                urls.update(find_urls(pt))
            except: continue
    else:
        try:
            pt = msg.get_payload(decode=True).decode(errors='ignore')
            text += pt
            urls.update(find_urls(pt))
        except: pass
    return text, list(urls)

def detect_sources(text, urls):
    sources = []
    seen = set()
    
    # ArXiv
    for m in re.finditer(r"(?:arXiv:|arxiv\.org/abs/|arxiv\.org/pdf/)\s*(\d{4}\.\d{4,5})", text, re.IGNORECASE):
        aid = m.group(1)
        if aid not in seen:
            sources.append({"type": "arxiv", "id": aid, "url": f"https://arxiv.org/pdf/{aid}.pdf"})
            seen.add(aid)
    
    # DOI
    for m in re.finditer(r"(?:doi:|doi\.org/)\s*(10\.\d{4,9}/[-._;()/:A-Z0-9]+)", text, re.IGNORECASE):
        doi = m.group(1)
        if doi not in seen:
            try:
                link = get_oa_link(doi)
            except: link = None
            sources.append({"type": "doi", "id": doi, "url": link}) 
            seen.add(doi)

    block = ['muse.jhu.edu', 'sciencedirect.com/science/article/pii']
    
    for link in urls:
        try:
            clean_link = clean_google_url(link)
            l_lower = clean_link.lower()
            
            if any(x in l_lower for x in block): continue
            
            if l_lower.endswith('.pdf') or 'viewcontent.cgi' in l_lower:
                lid = hashlib.md5(clean_link.encode()).hexdigest()[:10]
                if lid not in seen:
                    sources.append({"type": "pdf_link", "id": f"link_{lid}", "url": clean_link})
                    seen.add(lid)
        except: continue
    return sources

def polite_wait(url):
    if not url: return
    try:
        dom = urlparse(url).netloc
        last = DOMAIN_LAST_ACCESSED.get(dom, 0)
        if time.time() - last < 5: time.sleep(5)
        DOMAIN_LAST_ACCESSED[dom] = time.time()
    except: pass

def get_safe_filename(pid, save_dir):
    safe_name = re.sub(r'[\\/*?:"<>|]', '_', pid)
    return os.path.join(save_dir, f"{safe_name}.pdf")

def fetch_content(item, save_dir):
    url = item.get('url')
    if url: url = clean_google_url(url)
    
    if not url:
        if item.get("type") == "doi":
            logger.info(f"    ℹ️ 无 PDF 链接，尝试抓取摘要...")
            return fetch_abstract_only(item)
        return None, "No URL", None
    
    polite_wait(url)
    logger.info(f"    🔍 [下载] {url[:50]}...")
    
    try:
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0.0.0 Safari/537.36"}
        r = requests.get(url, headers=headers, timeout=30, stream=True)
        
        if r.status_code == 429: return None, "Rate Limit", None
        
        ctype = r.headers.get('Content-Type', '').lower()
        if 'application/pdf' not in ctype and not url.lower().endswith('.pdf'):
             logger.warning(f"    ⚠️ 链接响应非 PDF ({ctype})，尝试 DOI 摘要补救...")
             if item.get("type") == "doi": return fetch_abstract_only(item)
             return None, "Not PDF", None

        fname = get_safe_filename(item['id'], save_dir)
        with open(fname, "wb") as f:
            for chunk in r.iter_content(8192): f.write(chunk)
        
        if os.path.getsize(fname) < 2000:
            logger.warning("    ⚠️ PDF 文件过小，尝试 DOI 摘要补救...")
            os.remove(fname)
            if item.get("type") == "doi": return fetch_abstract_only(item)
            return None, "File Too Small", None
            
        try:
            content = pymupdf4llm.to_markdown(fname)
            if len(content) < 500:
                os.remove(fname)
                if item.get("type") == "doi": return fetch_abstract_only(item)
                return None, "Content Empty", None
            return content, "PDF Full Text", fname
        except:
            if item.get("type") == "doi": return fetch_abstract_only(item)
            return None, "Parse Error", None
            
    except Exception as e:
        logger.error(f"    ⚠️ 下载异常: {e}，尝试摘要补救...", exc_info=False)
        if item.get("type") == "doi": return fetch_abstract_only(item)
        return None, str(e), None

@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10))
def fetch_abstract_only(source_data):
    try:
        w = cr.works(ids=source_data["id"])
        title = w['message'].get('title', [''])[0]
        abstract = re.sub(r'<[^>]+>', '', w['message'].get('abstract', '无摘要'))
        return f"TITLE: {title}\n\nABSTRACT: {abstract}", "ABSTRACT_ONLY", None
    except Exception as e:
        raise

@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=5, max=30))
def analyze_with_llm(content, ctype):
    if ctype == "ABSTRACT_ONLY":
        prompt = f"""你是一名学术研究助理。以下是一篇文献的【标题和摘要】（未获取到全文）。
        
        请仅根据摘要进行简要分析：
        1. 提取/确认真实中文标题。
        2. 总结核心内容（背景、方法、结论）。
        3. 明确标注【仅基于摘要分析】。
        4. 第一行格式要求：TITLE: <英文标题>

        内容：
        {content[:3000]}
        """
    else:
        prompt = f"""你是一名学术研究助理。请用【中文】深度分析以下文献全文。
        ❗重要：第一行务必输出真实英文标题，格式 "TITLE: <Title>"。
        任务：
        1. 提取真实标题。
        2. 深度分析背景、问题、方法、结论、创新点。
        3. 遇到图表时插入 

[Image of X]
。
        4. 输出 Markdown。

        来源：{ctype}
        内容：{content[:50000]}
        """
    
    try:
        res = client.chat.completions.create(
            model=LLM_MODEL_NAME, messages=[{"role": "user", "content": prompt}], temperature=0.3
        )
        txt = res.choices[0].message.content.strip()
        
        real_title = "Unknown"
        body = txt
        match = re.match(r"^TITLE:\s*(.*)", txt, re.IGNORECASE)
        if match:
            real_title = match.group(1).strip()
            body = txt.split('\n', 1)[1].strip()
        return real_title, body
    except Exception:
        raise

def send_email(subject, body, attach_files=[]):
    html = markdown.markdown(body, extensions=['extra'])
    
    # 🟢 修复：正确的正则
    html = re.sub(
        r'\]+)\]', 
        r'<div style="background:#eef;padding:10px;margin:10px 0;border:1px dashed #ccc;text-align:center;color:#666">🖼️ 图示建议：\1</div>', 
        html
    )
    
    full_html = f"""
    <html>
    <body style="font-family:sans-serif;max-width:800px;margin:auto;padding:20px;">
        <div style="background:#2c3e50;color:white;padding:20px;border-radius:8px;">
            <h1 style="margin:0">{subject}</h1>
            <p>{datetime.date.today()}</p>
        </div>
        {html}
        <hr>
        <p style="text-align:center;color:#888;font-size:12px">AI Research Assistant</p>
    </body>
    </html>
    """
    
    msg = MIMEMultipart()
    msg["Subject"] = subject
    msg["From"] = EMAIL_USER
    msg["To"] = EMAIL_USER
    msg.attach(MIMEText(full_html, "html", "utf-8"))
    
    for fpath in attach_files:
        if os.path.exists(fpath):
            try:
                with open(fpath, "rb") as f:
                    part = MIMEApplication(f.read(), Name=os.path.basename(fpath))
                    part['Content-Disposition'] = f'attachment; filename="{os.path.basename(fpath)}"'
                    msg.attach(part)
            except Exception as e:
                logger.warning(f"附件处理失败: {e}")
            
    try:
        with smtplib.SMTP_SSL(SMTP_SERVER, 465) as s:
            s.login(EMAIL_USER, EMAIL_PASS)
            s.sendmail(EMAIL_USER, EMAIL_USER, msg.as_string())
        logger.info("✅ 邮件发送成功")
        return True
    except Exception as e:
        logger.critical(f"邮件发送失败: {e}", exc_info=True)
        return False

# --- 🚀 主流程 ---

def run():
    logger.info(f"🎬 启动: {datetime.datetime.now()}")
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)
    os.makedirs(DATA_DIR, exist_ok=True)
    
    db = PaperDB(DB_FILE)
    logger.info(f"📚 数据库加载完毕，共 {len(db.data)} 条记录")

    # --- 1. 扫描邮件 ---
    try:
        mail = imaplib.IMAP4_SSL(IMAP_SERVER)
        mail.login(EMAIL_USER, EMAIL_PASS)
        mail.select("inbox")
        
        since = (datetime.date.today() - timedelta(days=2)).strftime("%d-%b-%Y")
        _, data = mail.search(None, f'(SINCE "{since}")')
        
        if data[0]:
            for eid in data[0].split():
                try:
                    _, h = mail.fetch(eid, "(BODY.PEEK[HEADER])")
                    subj = decode_header(email.message_from_bytes(h[0][1])["Subject"])[0][0]
                    if isinstance(subj, bytes): subj = subj.decode()
                    
                    if not any(k.lower() in subj.lower() for k in TARGET_SUBJECTS): continue
                    logger.info(f"🎯 命中: {subj[:20]}...")
                    
                    _, m = mail.fetch(eid, "(RFC822)")
                    body, urls = extract_body(email.message_from_bytes(m[0][1]))
                    sources = detect_sources(body, urls)
                    
                    if not sources:
                        titles = extract_titles_from_text(body)
                        for t in titles:
                            try:
                                doi, full = search_doi_by_title(t)
                                if doi: sources.append({"type": "doi", "id": doi, "url": get_oa_link(doi), "title": full})
                            except: pass

                    for s in sources:
                        pid = s.get('id') or hashlib.md5(s.get('url','').encode()).hexdigest()[:10]
                        s['id'] = pid
                        if 'title' not in s: s['title'] = get_metadata_safe(s)
                        if db.add_new(pid, s): logger.info(f"    ➕ 入库: {pid}")
                except Exception as e: logger.error(f"扫描单封邮件错误: {e}")
    except Exception as e:
        logger.critical(f"IMAP 连接或扫描严重错误: {e}", exc_info=True)

    # --- 2. 处理下载 ---
    to_download = db.get_pending_downloads(BATCH_SIZE)
    logger.info(f"📥 待下载队列: {len(to_download)} 篇")
    
    for item in to_download:
        pid = item['id']
        logger.info(f"Processing Download: {pid}")
        content, ctype, path = fetch_content(item, DOWNLOAD_DIR)
        
        if ctype == "PDF Full Text":
            db.update_status(pid, "DOWNLOADED", {"local_path": path, "content_type": ctype})
        elif ctype == "ABSTRACT_ONLY":
            db.update_status(pid, "ABSTRACT_ONLY", {"abstract_content": content, "content_type": ctype})
        else:
            logger.warning(f"    ❌ 下载失败: {ctype}")
            db.increment_retry(pid)
            db.update_status(pid, "DOWNLOAD_FAILED", {"error": ctype})

    # --- 3. 处理分析 ---
    to_analyze = db.get_pending_analysis(BATCH_SIZE) 
    logger.info(f"🤖 待分析队列: {len(to_analyze)} 篇")
    
    new_reports = []
    attachments = []
    
    for item in to_analyze:
        pid = item['id']
        logger.info(f"Processing Analysis: {pid}")
        
        content = ""
        ctype = item.get("content_type", "Unknown")
        
        if item["status"] == "DOWNLOADED":
            local_path = get_safe_filename(pid, DOWNLOAD_DIR)
            if not os.path.exists(local_path):
                logger.info("    ⚠️ 本地文件缺失，重新下载...")
                content, ctype, local_path = fetch_content(item, DOWNLOAD_DIR)
                if not local_path:
                    db.update_status(pid, "DOWNLOAD_FAILED")
                    continue
            try: content = pymupdf4llm.to_markdown(local_path)
            except: db.update_status(pid, "ANALYSIS_FAILED"); continue
            attachments.append(local_path)
            
        elif item["status"] == "ABSTRACT_ONLY":
            content = item.get("abstract_content", "")
            if not content:
                try:
                    content, _, _ = fetch_abstract_only(item)
                except:
                    db.increment_retry(pid)
                    continue
        
        try:
            real_title, analysis = analyze_with_llm(content, ctype)
            trans_title = translate_title(real_title) or "翻译失败"
            
            badge = ""
            if item["status"] == "ABSTRACT_ONLY":
                badge = "<span style='background:#fff3cd;color:#856404;padding:2px 6px;border-radius:4px;font-size:12px;margin-left:10px;'>⚠️ 仅摘要分析</span>"

            card = f"""
            <div style="background:white;padding:20px;margin-bottom:20px;border-radius:10px;border:1px solid #eee;box-shadow:0 2px 5px rgba(0,0,0,0.05);">
                <div style="font-size:18px;font-weight:bold;color:#2c3e50;border-bottom:2px solid #3498db;padding-bottom:10px;">{real_title} {badge}</div>
                <div style="background:#f0f7ff;padding:8px;margin:10px 0;border-left:4px solid #3498db;color:#555;font-weight:bold;">{trans_title}</div>
                <div>{analysis}</div>
            </div>
            """
            new_reports.append(card)
            
            db.update_status(pid, "ANALYZED", {
                "real_title": real_title,
                "trans_title": trans_title
            })
        except Exception as e:
            logger.error(f"分析异常: {e}")
            db.increment_retry(pid)
            db.update_status(pid, "ANALYSIS_FAILED")

    # --- 4. 发送邮件 ---
    if new_reports:
        zips = []
        curr_zip, curr_size = [], 0
        for f in attachments:
            sz = os.path.getsize(f)
            if curr_size + sz > MAX_EMAIL_ZIP_SIZE:
                zips.append(curr_zip)
                curr_zip, curr_size = [f], sz
            else:
                curr_zip.append(f)
                curr_size += sz
        if curr_zip: zips.append(curr_zip)
        
        full_body = "\n".join(new_reports)
        
        if not zips:
             send_email(f"🤖 AI 日报 (新:{len(new_reports)})", full_body)
        else:
            for i, zfiles in enumerate(zips):
                zname = f"papers_{i+1}.zip"
                with zipfile.ZipFile(zname, 'w', zipfile.ZIP_DEFLATED) as zf:
                    for f in zfiles: zf.write(f, os.path.basename(f))
                
                subj = f"🤖 AI 日报 (Part {i+1}/{len(zips)})"
                body = full_body if i==0 else "<h3>📎 附件补发</h3>"
                
                send_email(subj, body, [zname])
                if os.path.exists(zname): os.remove(zname)
                time.sleep(5)
    else:
        logger.info("☕ 本次无新分析结果")

    logger.info("✅ 完成")

if __name__ == "__main__":
    if SCHEDULER_MODE:
        logger.info("🔄 启动循环模式...")
        while True:
            try: run()
            except Exception as e: logger.critical(f"❌ 任务崩溃: {e}", exc_info=True)
            time.sleep(LOOP_INTERVAL_HOURS * 3600)
    else:
        run()
