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
        doi = match.group(1
