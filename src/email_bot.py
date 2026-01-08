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
from email.header import decode_header
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.application import MIMEApplication

# --- 🛠️ 1. 核心配置区 ---
LLM_API_KEY = os.environ.get("LLM_API_KEY")
LLM_BASE_URL = "https://api.siliconflow.cn/v1"
LLM_MODEL_NAME = os.environ.get("LLM_MODEL_NAME", "deepseek-ai/DeepSeek-R1-distill-llama-70b")

EMAIL_USER = os.environ.get("EMAIL_USER")
EMAIL_PASS = os.environ.get("EMAIL_PASS")
IMAP_SERVER = "imap.gmail.com"
SMTP_SERVER = "smtp.gmail.com"

TARGET_SUBJECTS = [
    "文献鸟", "Google Scholar Alert", "ArXiv", "Project MUSE", 
    "new research", "Stork", "ScienceDirect", "Chinese politics", 
    "Imperial history", "Causal inference"
]

HISTORY_FILE = "data/history.json"
DOWNLOAD_DIR = "downloads"
MAX_ATTACHMENT_SIZE = 19 * 1024 * 1024
socket.setdefaulttimeout(60)

client = OpenAI(api_key=LLM_API_KEY, base_url=LLM_BASE_URL)
cr = Crossref()

# --- 🧠 2. 核心模块 ---

def get_oa_link_from_doi(doi):
    try:
        email_addr = "bot@example.com"
        r = requests.get(f"https://api.unpaywall.org/v2/{doi}?email={email_addr}", timeout=10)
        data = r.json()
        if data.get('is_oa') and data.get('best_oa_location'):
            return data['best_oa_location']['url_for_pdf']
    except: pass
    return None

def detect_and_extract_all(text):
    results = []
    seen_ids = set() 
    # 优化后的 ArXiv 匹配
    for match in re.finditer(r"(?:arXiv:|arxiv\.org/abs/|arxiv\.org/pdf/)\s*(\d{4}\.\d{4,5})", text, re.IGNORECASE):
        aid = match.group(1)
        if aid not in seen_ids:
            results.append({"type": "arxiv", "id": aid, "url": f"https://arxiv.org/pdf/{aid}.pdf"})
            seen_ids.add(aid)
    # DOI 匹配
    for match in re.finditer(r"doi:\s*(10\.\d{4,9}/[-._;()/:A-Z0-9]+)", text, re.IGNORECASE):
        doi = match.group(1)
        if doi not in seen_ids:
            oa_url = get_oa_link_from_doi(doi)
            results.append({"type": "doi", "id": doi, "url": oa_url})
            seen_ids.add(doi)
    return results

def fetch_content(source_data, save_dir=None):
    # 🟢 修改点：针对 ArXiv 增加 5 秒安全延迟，防止 429 报错
    if source_data.get("type") == "arxiv":
        print(f"    ⏳ [防封禁] 等待 ArXiv 响应 (5s)...")
        time.sleep(5)

    if source_data.get("url") and source_data["url"].endswith(".pdf"):
        print(f"    📥 [下载中] {source_data['url']}")
        try:
            headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
            r = requests.get(source_data["url"], headers=headers, timeout=60)
            if r.status_code == 200:
                file_id = source_data.get('id') or hashlib.md5(source_data['url'].encode()).hexdigest()
                safe_name = re.sub(r'[\\/*?:"<>|]', '_', file_id)
                filename = os.path.join(save_dir, f"{safe_name}.pdf") if save_dir else f"temp_{safe_name}.pdf"
                with open(filename, "wb") as f: f.write(r.content)
                content = pymupdf4llm.to_markdown(filename)
                return content, "PDF Full Text", filename
        except Exception as e:
            print(f"    ⚠️ PDF 下载失败: {e}")

    if source_data["type"] == "doi":
        try:
            work = cr.works(ids=source_data["id"])
            title = work['message'].get('title', [''])[0]
            abstract = re.sub(r'<[^>]+>', '', work['message'].get('abstract', '无摘要'))
            content = f"# {title}\n\n## Abstract\n{abstract}"
            return content, "Abstract Only", None
        except: pass
    return None, "Unknown", None

def analyze_with_llm(content, content_type, source_url=""):
    prompt = f"""
    请作为学术助手，深度分析以下文献。
    【来源】：{content_type} | 【链接】：{source_url}
    ### 🎨 视觉增强：在解释核心机制或数据时，插入 1-2 个 

[Image of X]
 标签。
    ### 📝 任务：Markdown 输出基本信息、背景、理论假设、数据方法(IV/DID等)、实证结果、结论贡献及局限。
    ---
    {content[:50000]}
    ---
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

def simple_translate(text):
    if not text or len(text) < 5: return text
    try:
        completion = client.chat.completions.create(
            model=LLM_MODEL_NAME,
            messages=[{"role": "system", "content": "Translate title to Chinese."}, {"role": "user", "content": text}],
            temperature=0.3
        )
        return completion.choices[0].message.content.strip()
    except: return text

# --- 📧 3. 辅助功能 ---

def load_history():
    if os.path.exists(HISTORY_FILE):
        try:
            with open(HISTORY_FILE, "r", encoding="utf-8") as f: return json.load(f)
        except: return []
    return []

def save_history(history_list):
    os.makedirs(os.path.dirname(HISTORY_FILE), exist_ok=True)
    with open(HISTORY_FILE, "w", encoding="utf-8") as f: json.dump(history_list, f, indent=2, ensure_ascii=False)

def get_unique_id(source_data):
    return source_data.get("id") or hashlib.md5(source_data.get("url", "").encode()).hexdigest()

def extract_body(msg):
    body_text = ""
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain" and "attachment" not in str(part.get("Content-Disposition")):
                try: body_text += part.get_payload(decode=True).decode(errors='ignore') + "\n"
                except: pass
    else:
        try: body_text += msg.get_payload(decode=True).decode(errors='ignore')
        except: pass
    return body_text

def send_email_with_attachment(subject, body, attachment_zip=None):
    msg = MIMEMultipart()
    msg["Subject"], msg["From"], msg["To"] = subject, EMAIL_USER, EMAIL_USER
    msg.attach(MIMEText(body, "markdown", "utf-8"))
    if attachment_zip and os.path.exists(attachment_zip):
        with open(attachment_zip, "rb") as f:
            part = MIMEApplication(f.read(), Name=os.path.basename(attachment_zip))
            part['Content-Disposition'] = f'attachment; filename="{os.path.basename(attachment_zip)}"'
            msg.attach(part)
    try:
        with smtplib.SMTP_SSL(SMTP_SERVER, 465) as server:
            server.login(EMAIL_USER, EMAIL_PASS)
            server.sendmail(EMAIL_USER, EMAIL_USER, msg.as_string())
        return True
    except: return False

# --- 🚀 4. 主逻辑 ---

def main():
    if os.path.exists(DOWNLOAD_DIR): shutil.rmtree(DOWNLOAD_DIR)
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)
    processed_ids = load_history()
    
    mail = imaplib.IMAP4_SSL(IMAP_SERVER)
    mail.login(EMAIL_USER, EMAIL_PASS)
    mail.select("inbox")
    date_str = (datetime.date.today() - datetime.timedelta(days=1)).strftime("%d-%b-%Y")
    _, data = mail.search(None, f'(SINCE "{date_str}")')
    
    pending_sources = []
    for e_id in data[0].split():
        _, m_data = mail.fetch(e_id, "(RFC822)")
        msg = email.message_from_bytes(m_data[0][1])
        subj, enc = decode_header(msg["Subject"])[0]
        subj = subj.decode(enc or 'utf-8') if isinstance(subj, bytes) else subj
        if any(k.lower() in subj.lower() for k in TARGET_SUBJECTS):
            sources = detect_and_extract_all(extract_body(msg))
            for s in sources:
                if get_unique_id(s) not in processed_ids: pending_sources.append(s)

    # 🟢 修改点：限制单次处理 15 篇，防止 GitHub Actions 超时
    MAX_PAPERS = 15
    to_process = pending_sources[:MAX_PAPERS]
    if not to_process:
        print("☕ 没有新文献需要处理。")
        return

    print(f"🚀 开始处理 {len(to_process)} 篇新文献...")
    report_body, all_files, total_new, failed = "", [], 0, []

    for src in to_process:
        uid = get_unique_id(src)
        content, ctype, path = fetch_content(src, save_dir=DOWNLOAD_DIR)
        if path: all_files.append(path)
        if content:
            ans = analyze_with_llm(content, ctype, src.get('url'))
            if "LLM 分析出错" not in ans:
                report_body += f"## 📑 {src.get('id', 'Paper')}\n\n{ans}\n\n---\n\n"
                processed_ids.append(uid)
                total_new += 1
                continue
        failed.append(src)

    final_report = f"# 📅 文献日报 {datetime.date.today()}\n\n" + report_body
    if total_new > 0 or failed:
        subj = f"🤖 AI 学术日报 (新:{total_new} 失败:{len(failed)})"
        zip_file = None
        if all_files:
            zip_file = "papers.zip"
            with zipfile.ZipFile(zip_file, 'w') as zf:
                for f in all_files: zf.write(f, os.path.basename(f))
        
        send_email_with_attachment(subj, final_report, zip_file)
        if zip_file and os.path.exists(zip_file): os.remove(zip_file)
        save_history(processed_ids)
        print("🎉 任务圆满完成！")

if __name__ == "__main__":
    main()
