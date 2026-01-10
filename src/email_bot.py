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
import random
from email.header import decode_header
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.application import MIMEApplication
from urllib.parse import unquote, urlparse
import markdown

# --- 🛠️ 1. 核心配置区 ---
LLM_API_KEY = os.environ.get("LLM_API_KEY")
LLM_BASE_URL = "https://api.siliconflow.cn/v1"
LLM_MODEL_NAME = os.environ.get("LLM_MODEL_NAME", "deepseek-ai/DeepSeek-R1-distill-llama-70b")

EMAIL_USER = os.environ.get("EMAIL_USER")
EMAIL_PASS = os.environ.get("EMAIL_PASS")
IMAP_SERVER = "imap.gmail.com"
SMTP_SERVER = "smtp.gmail.com"

# 🟢 运行模式
SCHEDULER_MODE = False 
LOOP_INTERVAL_HOURS = 4
BATCH_SIZE = 20

# 监控关键词
TARGET_SUBJECTS = [
    "文献鸟", "Google Scholar Alert", "ArXiv", "Project MUSE", 
    "new research", "Stork", "ScienceDirect", "Chinese politics", 
    "Imperial history", "Causal inference", "new results", "The Accounting Review",
    "recommendations available", "Table of Contents"
]

# 🟢 数据文件路径
DATA_DIR = "data"
HISTORY_0_FILE = os.path.join(DATA_DIR, "history0_scanned.json")
QUEUE_FILE = os.path.join(DATA_DIR, "queue_pending.json")
HISTORY_3_FILE = os.path.join(DATA_DIR, "history3_downloaded.json")
HISTORY_2_FILE = os.path.join(DATA_DIR, "history2_analyzed.json")
HISTORY_PROCESSED_ID_FILE = os.path.join(DATA_DIR, "history_processed_ids.json")

DOWNLOAD_DIR = "downloads"
MAX_EMAIL_ZIP_SIZE = 18 * 1024 * 1024 

socket.setdefaulttimeout(30)

client = OpenAI(api_key=LLM_API_KEY, base_url=LLM_BASE_URL)
cr = Crossref()

DOMAIN_LAST_ACCESSED = {}

# --- 🎨 邮件样式 ---
EMAIL_CSS = """
<style>
    body { font-family: 'Segoe UI', Roboto, Helvetica, Arial, sans-serif; line-height: 1.6; color: #333; max-width: 900px; margin: 0 auto; padding: 20px; background-color: #f9f9f9; }
    .header-box { background: linear-gradient(135deg, #2c3e50, #4ca1af); color: white; padding: 20px; border-radius: 8px; margin-bottom: 30px; box-shadow: 0 4px 6px rgba(0,0,0,0.1); }
    .header-box h1 { margin: 0; font-size: 24px; color: white; }
    .queue-info { background-color: rgba(255,255,255,0.2); padding: 5px 10px; border-radius: 4px; font-size: 0.9em; margin-top: 10px; display: inline-block; }
    .paper-card { background: white; padding: 25px; margin-bottom: 30px; border-radius: 10px; box-shadow: 0 2px 10px rgba(0,0,0,0.05); border: 1px solid #eee; }
    .paper-title { font-size: 20px; color: #2c3e50; font-weight: 700; margin-bottom: 5px; border-bottom: 2px solid #3498db; padding-bottom: 10px; }
    .paper-trans-title { font-size: 16px; color: #555; font-weight: 600; margin-bottom: 20px; background-color: #f0f7ff; padding: 8px; border-left: 4px solid #3498db; border-radius: 0 4px 4px 0; }
    h2 { font-size: 18px; color: #e67e22; margin-top: 25px; border-left: 4px solid #e67e22; padding-left: 10px; }
    .image-placeholder { background-color: #e8f6f3; border: 1px dashed #16a085; color: #16a085; padding: 10px; text-align: center; border-radius: 6px; margin: 15px 0; font-size: 0.9em; }
    .failed-section { background-color: #fff5f5; padding: 20px; border-radius: 8px; border: 1px solid #ffcccc; margin-top: 40px; }
    .failed-item { background: white; padding: 15px; margin-bottom: 10px; border-radius: 6px; border-left: 3px solid #ccc; font-size: 0.9em; }
    .failed-abstract { font-style: italic; color: #666; margin-top: 5px; background: #f9f9f9; padding: 5px; }
    .warning-box { background-color: #fff3cd; color: #856404; padding: 10px; border: 1px solid #ffeeba; border-radius: 5px; margin-top: 20px; font-weight: bold; }
    a { color: #3498db; text-decoration: none; }
    hr { border: 0; height: 1px; background: #eee; margin: 30px 0; }
</style>
"""

# --- 🧠 2. 核心模块 ---

def translate_title(text):
    if not text or len(text) < 5: return ""
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
    if title: return title
    s_id = source_data.get('id', '')
    s_type = source_data.get('type', '')
    if s_type == 'doi':
        try:
            w = cr.works(ids=s_id)
            title = w['message'].get('title', [''])[0]
        except: pass
    elif s_type == 'arxiv':
        title = f"ArXiv Paper {s_id}"
    return title or "Unknown Title"

def get_oa_link_from_doi(doi):
    try:
        email_addr = "bot@example.com"
        r = requests.get(f"https://api.unpaywall.org/v2/{doi}?email={email_addr}", timeout=15)
        data = r.json()
        if data.get('is_oa') and data.get('best_oa_location'):
            return data['best_oa_location']['url_for_pdf']
    except: pass
    return None

def extract_titles_from_text(text):
    print("    🧠 [智能提取] 正在分析邮件正文提取标题...")
    prompt = f"""
    You are a research assistant. Extract the titles of academic papers from the email text below.
    Rules:
    1. Ignore generic text like "Table of Contents", "Read the full article", "Unsubscribe".
    2. Return ONLY a JSON list of strings. Example: ["Title 1", "Title 2"].
    3. Do not output Markdown.
    
    Email Text:
    {text[:5000]}
    """
    try:
        completion = client.chat.completions.create(
            model=LLM_MODEL_NAME,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.1
        )
        content = completion.choices[0].message.content.strip()
        content = content.replace("```json", "").replace("```", "").strip()
        return json.loads(content)
    except Exception as e:
        print(f"    ⚠️ 标题提取失败: {e}")
        return []

def search_doi_by_title(title):
    print(f"    🔍 [Crossref] 搜索 DOI: {title[:40]}...")
    try:
        results = cr.works(query=title, limit=1)
        if results['message']['items']:
            item = results['message']['items'][0]
            return item.get('DOI'), item.get('title', [title])[0]
    except Exception as e: pass
    return None, None

def extract_body(msg):
    body_text = ""
    extracted_urls = set()
    def find_urls_in_text(text):
        urls = re.findall(r'(https?://[^\s"\'<>]+)', text)
        return [u.rstrip('.,;)]}') for u in urls]

    if msg.is_multipart():
        for part in msg.walk():
            content_type = part.get_content_type()
            disposition = str(part.get("Content-Disposition"))
            try:
                payload = part.get_payload(decode=True)
                if not payload: continue
                part_text = payload.decode(errors='ignore')
                if "attachment" not in disposition:
                    if content_type == "text/html":
                        hrefs = re.findall(r'href=["\']([^"\']+)["\']', part_text, re.IGNORECASE)
                        extracted_urls.update(hrefs)
                        clean_text = re.sub('<[^<]+?>', ' ', part_text)
                        body_text += clean_text + "\n"
                    else:
                        body_text += part_text + "\n"
                extracted_urls.update(find_urls_in_text(part_text))
            except: continue
    else:
        try:
            payload = msg.get_payload(decode=True)
            if payload:
                text = payload.decode(errors='ignore')
                body_text += text
                extracted_urls.update(find_urls_in_text(text))
        except: pass
    return body_text, list(extracted_urls)

def detect_and_extract_all(text, all_links=None):
    results = []
    seen_ids = set()
    
    for match in re.finditer(r"(?:arXiv:|arxiv\.org/abs/|arxiv\.org/pdf/)\s*(\d{4}\.\d{4,5})", text, re.IGNORECASE):
        aid = match.group(1)
        if aid not in seen_ids:
            results.append({"type": "arxiv", "id": aid, "url": f"https://arxiv.org/pdf/{aid}.pdf"})
            seen_ids.add(aid)
    
    for match in re.finditer(r"(?:doi:|doi\.org/)\s*(10\.\d{4,9}/[-._;()/:A-Z0-9]+)", text, re.IGNORECASE):
        doi = match.group(1)
        if doi not in seen_ids:
            oa_url = get_oa_link_from_doi(doi)
            results.append({"type": "doi", "id": doi, "url": oa_url})
            seen_ids.add(doi)
    
    ACADEMIC_DOMAINS = [
        'emerald.com', 'researchgate.net', 'wiley.com', 'sciencedirect.com', 
        'springer.com', 'tandfonline.com', 'sagepub.com', 'jstor.org', 'oup.com', 
        'cambridge.org', 'egrove.olemiss.edu'
    ]
    BLOCKED_DOMAINS = [
        'muse.jhu.edu', 
        'sciencedirect.com/science/article/pii',
        'scholar.google.com/scholar_share',
        'google.com/url',
        'scholar.google.com/scholar_url'
    ]
    
    if all_links:
        for link in all_links:
            try:
                link = unquote(link)
                link_lower = link.lower()
                if any(x in link_lower for x in ['unsubscribe', 'privacy', 'manage', 'twitter', 'facebook', 'linkedin']): continue
                if any(blk in link_lower for blk in BLOCKED_DOMAINS): continue

                is_pdf = link_lower.endswith('.pdf') or '/pdf/' in link_lower
                is_repo_pdf = 'viewcontent.cgi' in link_lower
                is_content_pdf = 'content/pdf' in link_lower
                is_academic_web = any(d in link_lower for d in ACADEMIC_DOMAINS)
                is_edu = '.edu' in urlparse(link).netloc
                
                if is_pdf or is_repo_pdf or is_content_pdf or is_academic_web or is_edu:
                    link_hash = hashlib.md5(link.encode()).hexdigest()[:10]
                    if link_hash not in seen_ids:
                        if "scholar_url?url=" in link:
                            match = re.search(r'url=([^&]+)', link)
                            if match: link = unquote(match.group(1))
                        source_type = "direct_pdf" if (is_pdf or is_repo_pdf) else "academic_web"
                        results.append({"type": source_type, "id": f"link_{link_hash}", "url": link})
                        seen_ids.add(link_hash)
            except: continue
    return results

def polite_wait(url):
    try:
        if not url: return
        domain = urlparse(url).netloc
        last_time = DOMAIN_LAST_ACCESSED.get(domain, 0)
        cooldown = 5 + random.uniform(1, 3)
        if time.time() - last_time < cooldown: time.sleep(cooldown)
        DOMAIN_LAST_ACCESSED[domain] = time.time()
    except: pass

def fetch_content(source_data, save_dir=None):
    if source_data.get("type") == "arxiv": time.sleep(3)
    url = source_data.get("url")
    if not url: 
        if source_data.get("type") == "doi": return fetch_abstract_only(source_data)
        return None, "No URL", None

    polite_wait(url)
    print(f"    🔍 [下载] 尝试访问: {url[:50]}...")
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36", "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8"}

    try:
        r = requests.get(url, headers=headers, timeout=30, allow_redirects=True, stream=True)
        if r.status_code == 429:
            print("    🛑 [429] 请求过多，冷却 60秒...")
            time.sleep(60)
            return None, "Rate Limited", None
            
        final_url = r.url
        content_type = r.headers.get('Content-Type', '').lower()
        is_pdf_response = ('application/pdf' in content_type or final_url.endswith('.pdf') or 'viewcontent.cgi' in final_url)

        if is_pdf_response:
            print("    📥 确认 PDF，下载中...")
            file_id = source_data.get('id') or hashlib.md5(url.encode()).hexdigest()[:10]
            safe_name = re.sub(r'[\\/*?:"<>|]', '_', file_id)
            filename = os.path.join(save_dir, f"{safe_name}.pdf")
            with open(filename, "wb") as f:
                for chunk in r.iter_content(chunk_size=8192): f.write(chunk)
            
            if os.path.getsize(filename) < 2000:
                print(f"    ⚠️ 文件过小，跳过。")
                os.remove(filename)
                return None, "Fake PDF", None

            try:
                content = pymupdf4llm.to_markdown(filename)
                if len(content) < 500:
                    print(f"    ⚠️ 解析内容过短，跳过。")
                    os.remove(filename)
                    return None, "Content Too Short", None
                print(f"    ✅ PDF 解析成功，长度: {len(content)}")
                return content, "PDF Full Text", filename
            except: return None, "PDF Error", None

        elif 'text/html' in content_type:
            print("    🌐 检测到网页，提取正文...")
            html_content = ""
            for chunk in r.iter_content(chunk_size=8192):
                html_content += chunk.decode(errors='ignore')
                if len(html_content) > 200000: break 
            text_content = re.sub(r'<[^<]+?>', '\n', html_content)
            text_content = re.sub(r'\n+', '\n', text_content).strip()
            if len(text_content) < 500:
                print(f"    ⚠️ 网页内容过短，跳过。")
                return None, "Content Too Short", None
            print(f"    ✅ 网页文本提取成功")
            return text_content, "Web Page Text", None

    except Exception as e:
        print(f"    ⚠️ 下载失败: {e}")
        if source_data.get("type") == "doi": return fetch_abstract_only(source_data)

    if source_data.get("type") == "doi": return fetch_abstract_only(source_data)
    return None, "Unknown", None

def fetch_abstract_only(source_data):
    try:
        print(f"    📚 [保底] 获取 Crossref 摘要...")
        work = cr.works(ids=source_data["id"])
        title = work['message'].get('title', [''])[0]
        abstract = re.sub(r'<[^>]+>', '', work['message'].get('abstract', '无摘要'))
        return abstract, "Abstract Only", None
    except: return None, "Error", None

def analyze_with_llm(content, content_type, source_url=""):
    # 🟢 提示词全中文，且要求中文输出
    prompt = f"""你是一名专业的社会科学/计算机科学研究助理。请用【中文】对以下文献内容进行深度分析。
    
    任务要求：
    1. 总结研究背景、核心问题、方法论和主要结论。
    2. 提取关键机制或理论创新点。
    3. 如果正文中有提到具体的图表或模型架构，请在解释该部分时插入 

[Image of X]
 标签（X为图表内容的简短英文描述）。
    4. 保持学术性，逻辑清晰。
    5. 输出格式为 Markdown。

    文献来源：{content_type}
    文章内容：
    {content[:50000]}
    """
    try:
        completion = client.chat.completions.create(
            model=LLM_MODEL_NAME,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.3
        )
        return completion.choices[0].message.content.strip()
    except Exception as e:
        return f"LLM 分析出错: {e}"

def generate_failed_report(failed_list):
    if not failed_list: return ""
    report = "\n\n<div class='failed-section'><h2>⚠️ 未获取全文的文献 (Skipped List)</h2>"
    report += "<p>以下文献因仅有摘要、触发反爬虫验证或文件无效，未进行深度分析。请点击链接查看原文：</p>"
    for src in failed_list:
        url = src.get('url', 'No URL')
        s_id = src.get('id', 'Unknown ID')
        sType = src.get('type', 'Unknown')
        title = src.get('title', s_id)
        
        abstract_text = src.get('abstract_content', '')
        if not abstract_text and sType == 'doi':
             try:
                w = cr.works(ids=s_id)
                abstract_text = re.sub(r'<[^<]+?>', '', w['message'].get('abstract', ''))
             except: pass
        
        report += f"<div class='failed-item'><h3>❌ {title}</h3>"
        report += f"<div class='failed-meta'>URL: <a href='{url}'>{url}</a> | Type: {sType}</div>"
        if abstract_text:
            report += f"<div class='failed-abstract'><strong>Abstract:</strong> {abstract_text[:400]}...</div>"
        report += "</div>"
    report += "</div>"
    return report

# --- 📧 3. 辅助与数据管理 ---

def load_json(filepath):
    if os.path.exists(filepath):
        try:
            with open(filepath, "r", encoding="utf-8") as f: return json.load(f)
        except: return []
    return []

def save_json(data, filepath):
    os.makedirs(os.path.dirname(filepath), exist_ok=True)
    with open(filepath, "w", encoding="utf-8") as f: json.dump(data, f, indent=2, ensure_ascii=False)

def append_to_history(new_items, filepath):
    history = load_json(filepath)
    existing_ids = {item['id'] for item in history}
    added_count = 0
    for item in new_items:
        if item['id'] not in existing_ids:
            history.append(item)
            existing_ids.add(item['id'])
            added_count += 1
    if added_count > 0:
        save_json(history, filepath)
    return added_count

def get_unique_id(source_data):
    return source_data.get("id") or hashlib.md5(source_data.get("url", "").encode()).hexdigest()

def send_email_with_attachment(subject, body_content, attachment_zip=None):
    try:
        html_content = markdown.markdown(body_content, extensions=['extra', 'tables', 'fenced_code'])
    except: 
        html_content = body_content
    
    try:
        def replacer(match): return f'<div class="image-placeholder">🖼️ 图示建议：{match.group(1)}</div>'
        html_content = re.sub(r'\]+)\]', replacer, html_content)
    except: pass
    
    final_html = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset='UTF-8'>
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        {EMAIL_CSS}
    </head>
    <body>
        <div class="header-box">
            <h1>{subject}</h1>
            <div class="queue-info">{datetime.date.today()} 生成</div>
        </div>
        {html_content}
        <hr>
        <p style='text-align:center;color:#888;font-size:12px;'>Generated by AI Research Assistant</p>
    </body>
    </html>
    """
    
    msg = MIMEMultipart()
    msg["Subject"] = subject
    msg["From"] = EMAIL_USER
    msg["To"] = EMAIL_USER
    msg.attach(MIMEText(final_html, "html", "utf-8"))
    
    if attachment_zip and os.path.exists(attachment_zip):
        if os.path.getsize(attachment_zip) > MAX_EMAIL_ZIP_SIZE:
            attach_note = f"<div class='warning-box'>⚠️ 附件过大 ({os.path.getsize(attachment_zip)/1024/1024:.1f}MB)，已自动移除。</div>"
            final_html = final_html.replace("<body>", f"<body>{attach_note}")
            msg = MIMEMultipart()
            msg["Subject"] = subject
            msg["From"] = EMAIL_USER
            msg["To"] = EMAIL_USER
            msg.attach(MIMEText(final_html, "html", "utf-8"))
        else:
            try:
                with open(attachment_zip, "rb") as f:
                    part = MIMEApplication(f.read(), Name=os.path.basename(attachment_zip))
                    part['Content-Disposition'] = f'attachment; filename="{os.path.basename(attachment_zip)}"'
                    msg.attach(part)
            except: pass
    
    try:
        with smtplib.SMTP_SSL(SMTP_SERVER, 465) as server:
            server.login(EMAIL_USER, EMAIL_PASS)
            server.sendmail(EMAIL_USER, EMAIL_USER, msg.as_string())
        return True
    except Exception as e:
        print(f"发送失败: {e}")
        return False

# --- 🚀 4. 主任务流程 ---

def run_task():
    print(f"🎬 任务启动: {datetime.datetime.now()}")
    if os.path.exists(DOWNLOAD_DIR): shutil.rmtree(DOWNLOAD_DIR)
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)
    os.makedirs(DATA_DIR, exist_ok=True)
    
    processed_ids = set(load_json(HISTORY_PROCESSED_ID_FILE))
    if not processed_ids:
        h0 = load_json(HISTORY_0_FILE)
        processed_ids = {item['id'] for item in h0}
    
    mail = imaplib.IMAP4_SSL(IMAP_SERVER)
    mail.login(EMAIL_USER, EMAIL_PASS)
    mail.select("inbox")
    
    date_criteria = (datetime.date.today() - timedelta(days=2)).strftime("%d-%b-%Y")
    print(f"🔍 搜索 {date_criteria} 之后的邮件...")
    _, data = mail.search(None, f'(SINCE "{date_criteria}")')
    email_list = data[0].split()
    print(f"📨 检索到 {len(email_list)} 封候选邮件")
    
    queue_pending = load_json(QUEUE_FILE)
    queue_ids = {item['id'] for item in queue_pending}
    print(f"📂 当前队列待办数: {len(queue_pending)}")
    
    for idx, e_id in enumerate(email_list):
        try:
            time.sleep(1)
            _, header_data = mail.fetch(e_id, "(BODY.PEEK[HEADER])")
            msg_header = email.message_from_bytes(header_data[0][1])
            subj, enc = decode_header(msg_header["Subject"])[0]
            subj = subj.decode(enc or 'utf-8') if isinstance(subj, bytes) else subj
            
            date_tuple = email.utils.parsedate_tz(msg_header["Date"])
            email_date_str = datetime.datetime.fromtimestamp(email.utils.mktime_tz(date_tuple)).strftime("%Y-%m-%d") if date_tuple else "Unknown"

            if not any(k.lower() in subj.lower() for k in TARGET_SUBJECTS): continue
            
            print(f"🎯 命中: 【{email_date_str}】{subj[:30]}...")
            _, m_data = mail.fetch(e_id, "(RFC822)")
            msg = email.message_from_bytes(m_data[0][1])
            
            body_text, all_urls = extract_body(msg)
            sources = detect_and_extract_all(body_text, all_urls)
            
            if not sources:
                print("    💡 启用 LLM 标题反查...")
                titles = extract_titles_from_text(body_text)
                for t in titles:
                    doi, full_title = search_doi_by_title(t)
                    if doi:
                        print(f"    ✅ 反查 DOI: {doi}")
                        oa_url = get_oa_link_from_doi(doi)
                        sources.append({"type": "doi", "id": doi, "url": oa_url, "title": full_title})
                        time.sleep(1)
                    else:
                        print(f"    ❌ 未找到 DOI: {t[:40]}...")

            new_in_queue = 0
            for s in sources:
                u_id = get_unique_id(s)
                s['id'] = u_id
                if 'title' not in s: s['title'] = get_metadata_safe(s)
                
                if u_id not in processed_ids and u_id not in queue_ids:
                    s['timestamp_added'] = str(datetime.datetime.now())
                    queue_pending.append(s)
                    queue_ids.add(u_id)
                    new_in_queue += 1
                    append_to_history([{
                        "id": u_id, "type": s.get('type'), "url": s.get('url'), 
                        "title": s.get('title'), "timestamp": str(datetime.datetime.now())
                    }], HISTORY_0_FILE)
            
            if new_in_queue > 0:
                print(f"    ➕ 新增 {new_in_queue} 篇到待办队列")

        except Exception as e:
            print(f"⚠️ 扫描错误: {e}")
            continue
            
    save_json(queue_pending, QUEUE_FILE)
    
    if not queue_pending:
        print("☕ 队列为空，无任务处理。")
        try: mail.logout() 
        except: pass
        return

    to_process = queue_pending[:BATCH_SIZE]
    remaining_queue = queue_pending[BATCH_SIZE:]
    
    print(f"🚀 开始处理本批次: {len(to_process)} 篇 (剩余: {len(remaining_queue)})")
    
    report_body = ""
    all_files, total_new, failed = [], 0, []
    history3_records = []
    history2_records = []
    processed_now = []

    for src in to_process:
        print(f"📝 处理: {src.get('id')}")
        if not src.get('trans_title') and src.get('title'):
             src['trans_title'] = translate_title(src['title'])
             print(f"    🇨🇳 标题翻译: {src['trans_title'][:20]}...")

        # 获取内容
        content, ctype, path = fetch_content(src, save_dir=DOWNLOAD_DIR)
        processed_now.append(src['id'])
        
        if path: 
            all_files.append(path)
            history3_records.append({
                "id": src['id'], "title": src.get('title'), 
                "trans_title": src.get('trans_title'), "timestamp": str(datetime.datetime.now())
            })
        
        # Abstract Only 判负
        if ctype == "Abstract Only":
            print("    ⚠️ 仅获取到摘要，跳过深度分析。")
            src['abstract_content'] = content
            failed.append(src)
            continue

        if content:
            print("🤖 AI 分析中...")
            ans = analyze_with_llm(content, ctype, src.get('url'))
            if "LLM 分析出错" not in ans:
                # 🟢 格式化：每篇论文用卡片包裹
                paper_html = f"""
                <div class="paper-card">
                    <div class="paper-title">{src.get('title', src['id'])}</div>
                    <div class="paper-trans-title">{src.get('trans_title', '（暂无中文标题）')}</div>
                    <div class="paper-content">{ans}</div>
                </div>
                """
                report_body += paper_html
                total_new += 1
                history2_records.append({
                    "id": src['id'], "title": src.get('title'),
                    "trans_title": src.get('trans_title'), "analysis_summary": ans[:100]+"...",
                    "timestamp": str(datetime.datetime.now())
                })
                continue
        failed.append(src)
    
    append_to_history(history3_records, HISTORY_3_FILE)
    append_to_history(history2_records, HISTORY_2_FILE)
    
    processed_ids.update(processed_now)
    save_json(list(processed_ids), HISTORY_PROCESSED_ID_FILE)
    
    queue_status = f"<div class='queue-info'>📊 队列状态：本批处理 {len(to_process)} 篇，剩余待办 {len(remaining_queue)} 篇。</div>"
    failed_report = generate_failed_report(failed)
    
    final_report_html = f"{queue_status}\n{report_body}\n{failed_report}"
    
    file_batches = []
    current_batch = []
    current_size = 0
    for f in all_files:
        try:
            f_size = os.path.getsize(f)
            if current_size + f_size > MAX_EMAIL_ZIP_SIZE:
                if current_batch: file_batches.append(current_batch)
                current_batch = [f]
                current_size = f_size
            else:
                current_batch.append(f)
                current_size += f_size
        except: pass
    if current_batch: file_batches.append(current_batch)
    
    # 🟢 邮件发送状态标记
    all_emails_sent = True 

    if not file_batches:
        if total_new > 0 or failed:
            print("📨 发送纯文本报告...")
            if not send_email_with_attachment(f"🤖 AI 学术日报 (新:{total_new})", final_report_html, None):
                all_emails_sent = False
    else:
        print(f"📨 附件过大，分 {len(file_batches)} 封发送...")
        for i, batch in enumerate(file_batches):
            zip_name = f"papers_part_{i+1}.zip"
            with zipfile.ZipFile(zip_name, 'w', zipfile.ZIP_DEFLATED) as zf:
                for f in batch: zf.write(f, os.path.basename(f))
            
            if i == 0:
                subject = f"🤖 AI 学术日报 (新:{total_new}) - Part 1"
                body = final_report_html
            else:
                subject = f"🤖 AI 学术日报 (附件 Part {i+1}) - {datetime.date.today()}"
                body = f"<div class='paper-card'><h3>📎 附件补发 (Part {i+1})</h3><p>这是后续的 PDF 附件包，请查收。</p></div>"
            
            if not send_email_with_attachment(subject, body, zip_name):
                all_emails_sent = False
            
            if os.path.exists(zip_name): os.remove(zip_name)
            time.sleep(10)
    
    # 🟢 关键：只有所有邮件发送成功，才更新队列文件
    if all_emails_sent:
        print(f"💾 [邮件发送成功] 更新队列：移除已处理 {len(to_process)} 篇，剩余 {len(remaining_queue)} 篇。")
        save_json(remaining_queue, QUEUE_FILE)
    else:
        print(f"⚠️ [邮件发送失败] 保留队列，等待下次重试。")

    try: mail.logout()
    except: pass
    print("✅ 本次任务完成")

def main():
    if SCHEDULER_MODE:
        print(f"🔄 已启动循环模式，每 {LOOP_INTERVAL_HOURS} 小时运行一次...")
        while True:
            try:
                run_task()
            except Exception as e:
                print(f"❌ 任务崩溃: {e}")
            print(f"💤 休眠 {LOOP_INTERVAL_HOURS} 小时...")
            time.sleep(LOOP_INTERVAL_HOURS * 3600)
    else:
        run_task()

if __name__ == "__main__":
    main()
