import os
import re
import requests
import pymupdf4llm
from openai import OpenAI
from habanero import Crossref
import time
import hashlib
import json
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
    """清洗 Google 跳转链接"""
    try:
        url = unquote(url)
        if "google" in url and ("url=" in url or "q=" in url):
            parsed = urlparse(url)
            qs = parse_qs(parsed.query)
            if 'url' in qs: 
                return unquote(qs['url'][0])
            if 'q' in qs: 
                return unquote(qs['q'][0])
    except Exception as e:
        logger.debug(f"URL 清洗异常: {e}")
    return url

# --- 数据库类 ---
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
            logger.error(f"保存数据库失败: {e}")

    def add_new(self, pid, meta):
        if pid not in self.data:
            self.data[pid] = {
                **meta, 
                "status": "NEW", 
                "retry": 0, 
                "created_at": str(datetime.datetime.now())
            }
            self.save()
            return True
        return False

    def update_status(self, pid, status, extra=None):
        if pid in self.data:
            self.data[pid]["status"] = status
            self.data[pid]["updated_at"] = str(datetime.datetime.now())
            if extra: 
                self.data[pid].update(extra)
            self.save()

    def get_pending_downloads(self, limit=BATCH_SIZE):
        res = []
        for pid, item in self.data.items():
            if item["status"] == "NEW":
                res.append(item)
            elif item["status"] == "DOWNLOAD_FAILED" and item.get("retry", 0) < MAX_RETRIES:
                res.append(item)
        return res[:limit]

    def get_pending_analysis(self, limit=BATCH_SIZE):
        res = []
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
    """翻译标题"""
    if not text or len(text) < 5 or "Unknown" in text: 
        return ""
    res = client.chat.completions.create(
        model=LLM_MODEL_NAME,
        messages=[{"role": "user", "content": f"请将以下学术论文标题翻译成中文（仅输出翻译后的文本）：{text}"}],
        temperature=0.1
    )
    return res.choices[0].message.content.strip()

def get_meta_safe(src):
    """安全获取元数据标题"""
    t = src.get('title', '')
    if t and "Unknown" not in t: 
        return t
    if src.get('type') == 'arxiv': 
        return f"ArXiv {src.get('id')}"
    return "Unknown Title"

def extract_titles(text):
    """从文本中提取标题"""
    logger.info("    🧠 [智能提取] 分析邮件标题...")
    try:
        res = client.chat.completions.create(
            model=LLM_MODEL_NAME, 
            messages=[{"role": "user", "content": f"Extract academic paper titles from the text below. Return ONLY a JSON list of strings. Text: {text[:3000]}"}],
            temperature=0.1
        )
        content = res.choices[0].message.content.strip()
        content = content.replace("```json", "").replace("```", "").strip()
        return json.loads(content)
    except Exception as e:
        logger.warning(f"标题提取失败: {e}")
        return []

@retry(stop=stop_after_attempt(3), wait=wait_exponential(min=4, max=20))
def search_doi(title):
    """通过标题搜索 DOI"""
    logger.info(f"    🔍 [Crossref] 搜索 DOI: {title[:30]}...")
    res = cr.works(query=title, limit=1)
    if res['message']['items']:
        it = res['message']['items'][0]
        return it.get('DOI'), it.get('title', [title])[0]
    return None, None

@retry(stop=stop_after_attempt(3), wait=wait_exponential(min=4, max=10))
def get_oa_link(doi):
    """获取开放获取链接"""
    r = requests.get(f"https://api.unpaywall.org/v2/{doi}?email=bot@example.com", timeout=10)
    if r.status_code == 200:
        d = r.json()
        if d.get('is_oa') and d.get('best_oa_location'): 
            return d['best_oa_location']['url_for_pdf']
    return None

def extract_body_urls(msg):
    """提取邮件正文和链接"""
    text = ""
    urls = set()
    
    def grep_url(t): 
        return [u.rstrip('.,;)]}') for u in re.findall(r'(https?://[^\s"\'<>]+)', t)]
    
    if msg.is_multipart():
        for p in msg.walk():
            try:
                payload = p.get_payload(decode=True)
                if not payload: 
                    continue
                content = payload.decode(errors='ignore')
                
                if p.get_content_type() == "text/html":
                    urls.update(re.findall(r'href=["\']([^"\']+)["\']', content, re.IGNORECASE))
                    text += re.sub('<[^<]+?>', ' ', content) + "\n"
                elif p.get_content_type() == "text/plain":
                    text += content + "\n"
                    
                urls.update(grep_url(content))
            except:
                continue
    else:
        try:
            payload = msg.get_payload(decode=True).decode(errors='ignore')
            text += payload
            urls.update(grep_url(payload))
        except:
            pass
    
    return text, list(urls)

def detect_sources(text, urls):
    """检测文献源"""
    srcs = []
    seen = set()
    
    # ArXiv
    for m in re.finditer(r"(?:arXiv:|arxiv\.org/abs/|arxiv\.org/pdf/)\s*(\d{4}\.\d{4,5})", text, re.I):
        aid = m.group(1)
        if aid not in seen:
            srcs.append({
                "type": "arxiv", 
                "id": aid, 
                "url": f"https://arxiv.org/pdf/{aid}.pdf"
            })
            seen.add(aid)
            
    # DOI
    for m in re.finditer(r"(?:doi:|doi\.org/)\s*(10\.\d{4,9}/[-._;()/:A-Z0-9]+)", text, re.I):
        doi = m.group(1)
        if doi not in seen:
            try: 
                link = get_oa_link(doi)
            except: 
                link = None
            srcs.append({"type": "doi", "id": doi, "url": link})
            seen.add(doi)
            
    # Links
    blocked = ['unsubscribe', 'twitter.com', 'facebook.com', 'muse.jhu.edu', 'sciencedirect.com/science/article/pii']
    
    for link in urls:
        try:
            clink = clean_google_url(link)
            if not clink: 
                continue
            lower = clink.lower()
            
            if any(x in lower for x in blocked): 
                continue
            
            if lower.endswith('.pdf') or 'viewcontent.cgi' in lower:
                lid = hashlib.md5(clink.encode()).hexdigest()[:10]
                if lid not in seen:
                    srcs.append({"type": "pdf_link", "id": f"link_{lid}", "url": clink})
                    seen.add(lid)
        except:
            continue
    
    return srcs

def get_path(pid):
    """获取安全的文件路径"""
    safe_name = re.sub(r'[\\/*?:"<>|]', '_', pid)
    return os.path.join(DOWNLOAD_DIR, f"{safe_name}.pdf")

def fetch_content(item):
    """下载文献内容"""
    url = item.get('url')
    if url:
        url = clean_google_url(url)
    
    if not url:
        if item.get("type") == "doi": 
            logger.info("    ℹ️ 无 PDF 链接，尝试抓取摘要...")
            return fetch_abstract(item)
        return None, "No URL", None
        
    logger.info(f"    🔍 [下载] {url[:50]}...")
    
    try:
        r = requests.get(
            url, 
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0.0.0"}, 
            timeout=30, 
            stream=True
        )
        
        if r.status_code == 429: 
            return None, "Rate Limit", None
        
        ct = r.headers.get('Content-Type', '').lower()
        if 'application/pdf' not in ct and not url.lower().endswith('.pdf'):
            logger.warning(f"    ⚠️ 响应非 PDF ({ct})，尝试摘要补救...")
            if item.get("type") == "doi": 
                return fetch_abstract(item)
            return None, "Not PDF", None
            
        fp = get_path(item['id'])
        with open(fp, "wb") as f:
            for chunk in r.iter_content(8192): 
                f.write(chunk)
            
        if os.path.getsize(fp) < 2000:
            logger.warning("    ⚠️ 文件过小，尝试摘要补救...")
            os.remove(fp)
            if item.get("type") == "doi": 
                return fetch_abstract(item)
            return None, "Too Small", None
            
        try:
            txt = pymupdf4llm.to_markdown(fp)
            if len(txt) < 500:
                os.remove(fp)
                if item.get("type") == "doi": 
                    return fetch_abstract(item)
                return None, "Empty", None
            return txt, "PDF", fp
        except Exception as e:
            logger.warning(f"    PDF 解析失败: {e}")
            if item.get("type") == "doi": 
                return fetch_abstract(item)
            return None, "Parse Error", None
            
    except Exception as e:
        logger.error(f"    下载异常: {e}")
        if item.get("type") == "doi": 
            return fetch_abstract(item)
        return None, str(e), None

@retry(stop=stop_after_attempt(3), wait=wait_exponential(min=2, max=10))
def fetch_abstract(item):
    """获取摘要（兜底方案）"""
    w = cr.works(ids=item["id"])
    t = w['message'].get('title', [''])[0]
    a = re.sub(r'<[^>]+>', '', w['message'].get('abstract', '无摘要'))
    return f"TITLE: {t}\n\nABSTRACT: {a}", "ABSTRACT_ONLY", None

@retry(stop=stop_after_attempt(3), wait=wait_exponential(min=5, max=30))
def analyze(txt, ctype):
    """LLM 分析文献"""
    if ctype == "ABSTRACT_ONLY":
        prompt = f"""你是学术研究助理。以下是文献的标题和摘要（未获取全文）。

请仅根据摘要进行简要分析：
1. 第一行输出真实英文标题，格式: TITLE: <英文标题>
2. 总结核心内容（背景、方法、结论）
3. 明确标注【仅基于摘要分析】
4. 输出 Markdown 格式

内容：
{txt[:3000]}
"""
    else:
        prompt = f"""你是学术研究助理。请用中文深度分析以下文献全文。

❗重要：第一行务必输出真实英文标题，格式 "TITLE: <Title>"

任务：
1. 提取真实标题
2. 深度分析背景、问题、方法、结论、创新点
3. 输出 Markdown 格式
4. 不要包含图片占位符

来源类型：{ctype}
内容：{txt[:50000]}
"""
    
    res = client.chat.completions.create(
        model=LLM_MODEL_NAME,
        messages=[{"role": "user", "content": prompt}],
        temperature=0.3
    )
    raw = res.choices[0].message.content.strip()
    
    title = "Unknown"
    body = raw
    m = re.match(r"^TITLE:\s*(.*)", raw, re.I)
    if m:
        title = m.group(1).strip()
        parts = raw.split('\n', 1)
        body = parts[1].strip() if len(parts) > 1 else ""
    
    return title, body

def send_mail(subj, md_body, files=None):
    """发送邮件"""
    if files is None:
        files = []
    
    html = markdown.markdown(md_body, extensions=['extra'])
    
    full_html = f"""
    <html>
    <body style="font-family:sans-serif;max-width:800px;margin:auto;padding:20px">
        <div style="background:#2c3e50;color:white;padding:20px;border-radius:8px">
            <h1 style="margin:0">{subj}</h1>
            <p>{datetime.date.today()}</p>
        </div>
        {html}
        <hr>
        <p style="text-align:center;color:#888;font-size:12px">AI Research Assistant</p>
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
            except Exception as e:
                logger.warning(f"附件处理失败: {e}")
            
    try:
        with smtplib.SMTP_SSL(SMTP_SERVER, 465) as s:
            s.login(EMAIL_USER, EMAIL_PASS)
            s.sendmail(EMAIL_USER, EMAIL_USER, msg.as_string())
        logger.info("✅ 邮件已发送")
        return True
    except Exception as e:
        logger.error(f"邮件发送失败: {e}")
        return False

# --- 主程序 ---
def run():
    """主运行函数"""
    logger.info(f"🎬 任务开始: {datetime.datetime.now()}")
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)
    os.makedirs(DATA_DIR, exist_ok=True)
    
    db = PaperDB(DB_FILE)
    logger.info(f"📚 数据库记录数: {len(db.data)}")

    # ========== 1. 扫描邮件 ==========
    try:
        m = imaplib.IMAP4_SSL(IMAP_SERVER)
        m.login(EMAIL_USER, EMAIL_PASS)
        m.select("inbox")
        
        since_date = (datetime.date.today() - timedelta(days=2)).strftime("%d-%b-%Y")
        _, data = m.search(None, f'(SINCE "{since_date}")')
        
        if data[0]:
            for eid in data[0].split():
                try:
                    _, h = m.fetch(eid, "(BODY.PEEK[HEADER])")
                    subj = decode_header(email.message_from_bytes(h[0][1])["Subject"])[0][0]
                    if isinstance(subj, bytes): 
                        subj = subj.decode()
                    
                    if not any(k.lower() in subj.lower() for k in TARGET_SUBJECTS): 
                        continue
                    
                    logger.info(f"🎯 命中邮件: {subj[:20]}...")
                    
                    _, b = m.fetch(eid, "(RFC822)")
                    msg = email.message_from_bytes(b[0][1])
                    txt, urls = extract_body_urls(msg)
                    srcs = detect_sources(txt, urls)
                    
                    # 如果没有检测到源，尝试智能提取
                    if not srcs:
                        ts = extract_titles(txt)
                        for t in ts:
                            try:
                                doi, full = search_doi(t)
                                if doi: 
                                    oa_link = get_oa_link(doi)
                                    srcs.append({
                                        "type": "doi", 
                                        "id": doi, 
                                        "url": oa_link,
                                        "title": full
                                    })
                            except Exception as e:
                                logger.warning(f"DOI 搜索失败: {e}")
                            
                    for s in srcs:
                        pid = s.get('id') or hashlib.md5(s.get('url', '').encode()).hexdigest()[:10]
                        s['id'] = pid
                        if 'title' not in s: 
                            s['title'] = get_meta_safe(s)
                        if db.add_new(pid, s): 
                            logger.info(f"    ➕ 新增: {pid}")
                            
                except Exception as e: 
                    logger.error(f"邮件解析错误: {e}")
                    
    except Exception as e: 
        logger.error(f"IMAP 连接错误: {e}", exc_info=True)

    # ========== 2. 下载文献 ==========
    pend_dl = db.get_pending_downloads(BATCH_SIZE)
    logger.info(f"📥 待下载队列: {len(pend_dl)}")
    
    for item in pend_dl:
        pid = item['id']
        logger.info(f"Processing Download: {pid}")
        
        res, type_, path = fetch_content(item)
        
        if type_ == "PDF":
            db.update_status(pid, "DOWNLOADED", {
                "local_path": path, 
                "content_type": type_
            })
        elif type_ == "ABSTRACT_ONLY":
            db.update_status(pid, "ABSTRACT_ONLY", {
                "abstract_content": res, 
                "content_type": type_
            })
        else:
            logger.warning(f"    ❌ 下载失败: {type_}")
            db.inc_retry(pid)
            db.update_status(pid, "DOWNLOAD_FAILED", {"error": type_})

    # ========== 3. 分析文献 ==========
    pend_an = db.get_pending_analysis(BATCH_SIZE)
    logger.info(f"🤖 待分析队列: {len(pend_an)}")
    
    reports, atts = [], []
    
    for item in pend_an:
        pid = item['id']
        logger.info(f"Processing Analysis: {pid}")
        
        txt, ctype = "", item.get("content_type", "Unknown")
        
        if item["status"] == "DOWNLOADED":
            fp = get_path(pid)
            if not os.path.exists(fp):
                logger.info("    ⚠️ 本地文件缺失，重新下载...")
                txt_new, ctype_new, fp_new = fetch_content(item)
                if not fp_new: 
                    db.update_status(pid, "DOWNLOAD_FAILED")
                    continue
                fp = fp_new
                
            try: 
                txt = pymupdf4llm.to_markdown(fp)
            except Exception as e:
                logger.error(f"Markdown 提取失败: {e}")
                db.update_status(pid, "ANALYSIS_FAILED")
                continue
                
            atts.append(fp)
            
        elif item["status"] == "ABSTRACT_ONLY":
            txt = item.get("abstract_content", "")
            if not txt:
                try: 
                    txt, _, _ = fetch_abstract(item)
                except Exception as e:
                    logger.error(f"摘要获取失败: {e}")
                    db.inc_retry(pid)
                    continue
        
        try:
            rt, ans = analyze(txt, ctype)
            tt = translate_title(rt) or "翻译失败"
            
            badge = ""
            if ctype == "ABSTRACT_ONLY":
                badge = "<span style='background:#fff3cd;color:#856404;padding:2px 6px;border-radius:4px;font-size:12px;margin-left:10px'>⚠️ 仅摘要分析</span>"
            
            card = f"""
<div style="background:white;padding:20px;margin-bottom:20px;border-radius:10px;border:1px solid #eee;box-shadow:0 2px 5px rgba(0,0,0,0.05)">
    <div style="font-size:18px;font-weight:bold;color:#2c3e50;border-bottom:2px solid #3498db;padding-bottom:10px">{rt} {badge}</div>
    <div style="background:#f0f7ff;padding:8px;margin:10px 0;border-left:4px solid #3498db;color:#555;font-weight:bold">{tt}</div>
    <div>{ans}</div>
</div>
"""
            reports.append(card)
            db.update_status(pid, "ANALYZED", {
                "real_title": rt, 
                "trans_title": tt
            })
            
        except Exception as e:
            logger.error(f"分析失败: {e}", exc_info=True)
            db.inc_retry(pid)
            db.update_status(pid, "ANALYSIS_FAILED")

    # ========== 4. 发送邮件 ==========
    if reports:
        body = "\n".join(reports)
        
        # 分卷打包附件
        zips = []
        cz, csz = [], 0
        for f in atts:
            s = os.path.getsize(f)
            if csz + s > MAX_EMAIL_ZIP_SIZE:
                zips.append(cz)
                cz, csz = [f], s
            else:
                cz.append(f)
                csz += s
        if cz: 
            zips.append(cz)
        
        # 发送邮件
        if not zips:
            send_mail(f"🤖 AI 日报 (新:{len(reports)})", body)
        else:
            for i, zf in enumerate(zips):
                zn = f"papers_{i+1}.zip"
                try:
                    with zipfile.ZipFile(zn, 'w', zipfile.ZIP_DEFLATED) as z:
                        for f in zf: 
                            z.write(f, os.path.basename(f))
                    
                    subj = f"🤖 AI 日报 (Part {i+1}/{len(zips)})"
                    b = body if i == 0 else "<h3>📎 附件补发</h3>"
                    
                    send_mail(subj, b, [zn])
                    
                    if os.path.exists(zn): 
                        os.remove(zn)
                    
                    time.sleep(5)
                    
                except Exception as e:
                    logger.error(f"ZIP 处理失败: {e}")
    else:
        logger.info("☕ 本次无新分析结果")
    
    logger.info("✅ 任务完成")

if __name__ == "__main__":
    if SCHEDULER_MODE:
        logger.info("🔄 启动循环模式...")
        while True:
            try: 
                run()
            except Exception as e:
                logger.exception("任务崩溃")
            time.sleep(LOOP_INTERVAL_HOURS * 3600)
    else:
        run()
