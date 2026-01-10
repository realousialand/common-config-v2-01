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
from datetime import timedelta
from email.header import decode_header
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.application import MIMEApplication
from urllib.parse import unquote, urlparse
import markdown

# --- 🛠️ 配置区 ---
LLM_API_KEY = os.environ.get("LLM_API_KEY")
LLM_BASE_URL = "https://api.siliconflow.cn/v1"
LLM_MODEL_NAME = os.environ.get("LLM_MODEL_NAME", "deepseek-ai/DeepSeek-R1-distill-llama-70b")

EMAIL_USER = os.environ.get("EMAIL_USER")
EMAIL_PASS = os.environ.get("EMAIL_PASS")
IMAP_SERVER = "imap.gmail.com"
SMTP_SERVER = "smtp.gmail.com"

# 🟢 每次处理上限（防止超时）
BATCH_SIZE = 20
MAX_RETRIES = 3 # 失败重试次数上限

TARGET_SUBJECTS = [
    "文献鸟", "Google Scholar Alert", "ArXiv", "Project MUSE", 
    "new research", "Stork", "ScienceDirect", "Chinese politics", 
    "Imperial history", "Causal inference", "new results", "The Accounting Review",
    "recommendations available", "Table of Contents"
]

DATA_DIR = "data"
DB_FILE = os.path.join(DATA_DIR, "papers_database.json") # 🟢 统一数据库
DOWNLOAD_DIR = "downloads"
MAX_EMAIL_ZIP_SIZE = 18 * 1024 * 1024 
socket.setdefaulttimeout(30)

client = OpenAI(api_key=LLM_API_KEY, base_url=LLM_BASE_URL)
cr = Crossref()
DOMAIN_LAST_ACCESSED = {}

# --- 📚 数据库管理类 (核心优化) ---
class PaperDB:
    def __init__(self, filepath):
        self.filepath = filepath
        self.data = self._load()

    def _load(self):
        if os.path.exists(self.filepath):
            try:
                with open(self.filepath, 'r', encoding='utf-8') as f:
                    return json.load(f)
            except: pass
        return {}

    def save(self):
        os.makedirs(os.path.dirname(self.filepath), exist_ok=True)
        with open(self.filepath, 'w', encoding='utf-8') as f:
            json.dump(self.data, f, indent=2, ensure_ascii=False)

    def add_new(self, pid, metadata):
        if pid not in self.data:
            self.data[pid] = {
                **metadata,
                "status": "NEW", # 初始状态
                "retry_count": 0,
                "created_at": str(datetime.datetime.now()),
                "history": []
            }
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
        # 获取 NEW 或者 下载失败且重试次数未超标的
        candidates = []
        for pid, item in self.data.items():
            if item["status"] == "NEW":
                candidates.append(item)
            elif item["status"] == "DOWNLOAD_FAILED" and item.get("retry_count", 0) < MAX_RETRIES:
                candidates.append(item)
        return candidates[:limit]

    def get_pending_analysis(self, limit=BATCH_SIZE):
        # 获取 DOWNLOADED 或者 分析失败且重试次数未超标的
        candidates = []
        for pid, item in self.data.items():
            if item["status"] == "DOWNLOADED":
                candidates.append(item)
            elif item["status"] == "ANALYSIS_FAILED" and item.get("retry_count", 0) < MAX_RETRIES:
                candidates.append(item)
        return candidates[:limit]

    def increment_retry(self, pid):
        if pid in self.data:
            self.data[pid]["retry_count"] = self.data[pid].get("retry_count", 0) + 1
            self.save()

# --- 🧠 核心功能 ---

def translate_title(text):
    if not text or len(text) < 5 or "Unknown" in text: return ""
    try:
        completion = client.chat.completions.create(
            model=LLM_MODEL_NAME,
            messages=[{"role": "user", "content": f"请将以下学术论文标题翻译成中文（仅输出翻译后的文本）：{text}"}],
            temperature=0.1
        )
        return completion.choices[0].message.content.strip()
    except: return ""

def get_metadata_safe(source_data):
    title = source_data.get('title', '')
    if title and "Unknown" not in title: return title
    s_id = source_data.get('id', '')
    if source_data.get('type') == 'arxiv': return f"ArXiv Paper {s_id}"
    return title or "Unknown Title"

def extract_titles_from_text(text):
    print("    🧠 [智能提取] 正在分析邮件正文提取标题...")
    prompt = f"Extract academic paper titles from the text below. Return ONLY a JSON list of strings. Text: {text[:3000]}"
    try:
        completion = client.chat.completions.create(
            model=LLM_MODEL_NAME, messages=[{"role": "user", "content": prompt}], temperature=0.1
        )
        content = completion.choices[0].message.content.strip().replace("```json", "").replace("```", "").strip()
        return json.loads(content)
    except: return []

def search_doi_by_title(title):
    print(f"    🔍 [Crossref] 搜索 DOI: {title[:30]}...")
    try:
        res = cr.works(query=title, limit=1)
        if res['message']['items']:
            item = res['message']['items'][0]
            return item.get('DOI'), item.get('title', [title])[0]
    except: pass
    return None, None

def get_oa_link(doi):
    try:
        r = requests.get(f"https://api.unpaywall.org/v2/{doi}?email=bot@example.com", timeout=10)
        data = r.json()
        if data.get('is_oa') and data.get('best_oa_location'):
            return data['best_oa_location']['url_for_pdf']
    except: pass
    return None

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
            link = get_oa_link(doi)
            sources.append({"type": "doi", "id": doi, "url": link}) # url可能为None
            seen.add(doi)

    # Direct Links
    block = ['muse.jhu.edu', 'scholar.google.com/scholar_share', 'google.com/url']
    for link in urls:
        try:
            l = unquote(link).lower()
            if any(x in l for x in block): continue
            if l.endswith('.pdf') or 'viewcontent.cgi' in l:
                lid = hashlib.md5(l.encode()).hexdigest()[:10]
                if lid not in seen:
                    sources.append({"type": "pdf_link", "id": f"link_{lid}", "url": link})
                    seen.add(lid)
        except: continue
    return sources

def polite_wait(url):
    if not url: return
    dom = urlparse(url).netloc
    last = DOMAIN_LAST_ACCESSED.get(dom, 0)
    if time.time() - last < 5: time.sleep(5)
    DOMAIN_LAST_ACCESSED[dom] = time.time()

def get_safe_filename(pid, save_dir):
    safe_name = re.sub(r'[\\/*?:"<>|]', '_', pid)
    return os.path.join(save_dir, f"{safe_name}.pdf")

def fetch_content(item, save_dir):
    url = item.get('url')
    if not url: return None, "No URL", None
    
    polite_wait(url)
    print(f"    🔍 [下载] {url[:50]}...")
    
    try:
        r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=30, stream=True)
        if r.status_code == 429: return None, "Rate Limit", None
        
        # 检查是否真是PDF
        if 'application/pdf' not in r.headers.get('Content-Type', '').lower() and not url.endswith('.pdf'):
             return None, "Not PDF", None

        fname = get_safe_filename(item['id'], save_dir)
        with open(fname, "wb") as f:
            for chunk in r.iter_content(8192): f.write(chunk)
        
        if os.path.getsize(fname) < 2000:
            os.remove(fname)
            return None, "File Too Small", None
            
        try:
            content = pymupdf4llm.to_markdown(fname)
            if len(content) < 500:
                os.remove(fname)
                return None, "Content Empty", None
            return content, "PDF Full Text", fname
        except:
            return None, "Parse Error", None
            
    except Exception as e:
        return None, str(e), None

def analyze_with_llm(content, ctype):
    prompt = f"""你是一名学术研究助理。请用【中文】分析以下文献。
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
    except Exception as e: return None, f"Error: {e}"

def send_email(subject, body, attach_files=[]):
    html = markdown.markdown(body, extensions=['extra'])
    # 替换 Image tag
    html = re.sub(r'\]+)\]', r'<div style="background:#eef;padding:10px;margin:10px 0;border:1px dashed #ccc;text-align:center;color:#666">🖼️ 图示建议：\1</div>', html)
    
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
            except: pass
            
    try:
        with smtplib.SMTP_SSL(SMTP_SERVER, 465) as s:
            s.login(EMAIL_USER, EMAIL_PASS)
            s.sendmail(EMAIL_USER, EMAIL_USER, msg.as_string())
        return True
    except Exception as e:
        print(f"邮件失败: {e}")
        return False

# --- 🚀 主流程 ---

def run():
    print(f"🎬 启动: {datetime.datetime.now()}")
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)
    os.makedirs(DATA_DIR, exist_ok=True)
    
    db = PaperDB(DB_FILE)
    print(f"📚 数据库加载完毕，共 {len(db.data)} 条记录")

    # --- 1. 扫描邮件 (生产者) ---
    mail = imaplib.IMAP4_SSL(IMAP_SERVER)
    mail.login(EMAIL_USER, EMAIL_PASS)
    mail.select("inbox")
    
    since = (datetime.date.today() - timedelta(days=2)).strftime("%d-%b-%Y")
    _, data = mail.search(None, f'(SINCE "{since}")')
    
    for eid in data[0].split():
        try:
            _, h = mail.fetch(eid, "(BODY.PEEK[HEADER])")
            subj = decode_header(email.message_from_bytes(h[0][1])["Subject"])[0][0]
            if isinstance(subj, bytes): subj = subj.decode()
            
            if not any(k.lower() in subj.lower() for k in TARGET_SUBJECTS): continue
            print(f"🎯 命中: {subj[:20]}...")
            
            _, m = mail.fetch(eid, "(RFC822)")
            body, urls = extract_body(email.message_from_bytes(m[0][1]))
            sources = detect_sources(body, urls)
            
            # 如果没找到链接，尝试LLM反查
            if not sources:
                titles = extract_titles_from_text(body)
                for t in titles:
                    doi, full = search_doi_by_title(t)
                    if doi: sources.append({"type": "doi", "id": doi, "url": get_oa_link(doi), "title": full})

            for s in sources:
                # 统一 ID 生成
                pid = s.get('id') or hashlib.md5(s.get('url','').encode()).hexdigest()[:10]
                s['id'] = pid
                if 'title' not in s: s['title'] = get_metadata_safe(s)
                
                # 添加到数据库 (NEW)
                if db.add_new(pid, s):
                    print(f"    ➕ 入库: {pid}")
                    
        except Exception as e: print(f"扫描错误: {e}")

    # --- 2. 处理下载 (消费者 1) ---
    to_download = db.get_pending_downloads(BATCH_SIZE)
    print(f"📥 待下载队列: {len(to_download)} 篇")
    
    for item in to_download:
        pid = item['id']
        print(f"Processing Download: {pid}")
        content, ctype, path = fetch_content(item, DOWNLOAD_DIR)
        
        if path:
            # 下载成功
            db.update_status(pid, "DOWNLOADED", {"local_path": path, "content_type": ctype})
        else:
            # 下载失败
            print(f"    ❌ 下载失败: {ctype}")
            db.increment_retry(pid)
            db.update_status(pid, "DOWNLOAD_FAILED", {"error": ctype})

    # --- 3. 处理分析 (消费者 2) ---
    # 注意：这里会重新获取 DOWNLOADED 状态的，包括刚刚下载成功的
    to_analyze = db.get_pending_analysis(BATCH_SIZE) 
    print(f"🤖 待分析队列: {len(to_analyze)} 篇")
    
    new_reports = []
    attachments = []
    
    for item in to_analyze:
        pid = item['id']
        print(f"Processing Analysis: {pid}")
        
        # 必须确保文件存在 (GitHub Actions 每次是新的，所以必须是刚才下载的)
        # 如果是之前运行下载的，但在当前环境里没有，需要重新下载
        local_path = get_safe_filename(pid, DOWNLOAD_DIR)
        if not os.path.exists(local_path):
            print("    ⚠️ 本地文件缺失 (可能是上次运行下载的)，重新下载...")
            content, ctype, local_path = fetch_content(item, DOWNLOAD_DIR)
            if not local_path:
                print("    ❌ 重试下载失败")
                db.update_status(pid, "DOWNLOAD_FAILED") # 回退状态
                continue
        
        # 读取内容
        try:
            content = pymupdf4llm.to_markdown(local_path)
        except:
            print("    ❌ 文件无法读取")
            db.update_status(pid, "ANALYSIS_FAILED")
            continue

        # LLM 分析
        real_title, analysis = analyze_with_llm(content, "PDF")
        
        if analysis and "Error" not in analysis:
            trans_title = translate_title(real_title)
            
            # 生成卡片 HTML
            card = f"""
            <div style="background:white;padding:20px;margin-bottom:20px;border-radius:10px;border:1px solid #eee;box-shadow:0 2px 5px rgba(0,0,0,0.05);">
                <div style="font-size:18px;font-weight:bold;color:#2c3e50;border-bottom:2px solid #3498db;padding-bottom:10px;">{real_title}</div>
                <div style="background:#f0f7ff;padding:8px;margin:10px 0;border-left:4px solid #3498db;color:#555;font-weight:bold;">{trans_title}</div>
                <div>{analysis}</div>
            </div>
            """
            new_reports.append(card)
            attachments.append(local_path)
            
            # 更新状态为 ANALYZED
            db.update_status(pid, "ANALYSIS_FAILED" if "Error" in analysis else "ANALYZED", {
                "real_title": real_title,
                "trans_title": trans_title
            })
        else:
            db.increment_retry(pid)
            db.update_status(pid, "ANALYSIS_FAILED")

    # --- 4. 发送邮件 ---
    if new_reports:
        # 分包发送
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
        
        for i, zfiles in enumerate(zips):
            zname = f"papers_{i+1}.zip"
            with zipfile.ZipFile(zname, 'w', zipfile.ZIP_DEFLATED) as zf:
                for f in zfiles: zf.write(f, os.path.basename(f))
            
            subj = f"🤖 AI 日报 (Part {i+1}/{len(zips)})"
            body = full_body if i==0 else "<h3>📎 附件补发</h3>"
            
            send_email(subj, body, zname)
            if os.path.exists(zname): os.remove(zname)
            time.sleep(5)
    else:
        print("☕ 本次无新分析结果")

    print("✅ 完成")

if __name__ == "__main__":
    run()
