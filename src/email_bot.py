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

# 监控关键词
TARGET_SUBJECTS = [
    "文献鸟", "Google Scholar Alert", "ArXiv", "Project MUSE", 
    "new research", "Stork", "ScienceDirect", "Chinese politics", 
    "Imperial history", "Causal inference", "new results", "The Accounting Review",
    "recommendations available", "Table of Contents"
]

HISTORY_FILE = "data/history.json"
DOWNLOAD_DIR = "downloads"
MAX_ATTACHMENT_SIZE = 19 * 1024 * 1024
socket.setdefaulttimeout(30)

client = OpenAI(api_key=LLM_API_KEY, base_url=LLM_BASE_URL)
cr = Crossref()

DOMAIN_LAST_ACCESSED = {}

# --- 🎨 邮件样式 ---
EMAIL_CSS = """
<style>
    body { font-family: 'Helvetica Neue', Helvetica, Arial, sans-serif; line-height: 1.6; color: #333; max-width: 800px; margin: 0 auto; padding: 20px; }
    h1 { color: #2c3e50; border-bottom: 2px solid #3498db; padding-bottom: 10px; font-size: 24px; }
    h2 { color: #e67e22; margin-top: 30px; font-size: 20px; border-left: 5px solid #e67e22; padding-left: 10px; background-color: #fdf2e9; }
    .image-placeholder { background-color: #e8f6f3; border: 1px dashed #1abc9c; color: #16a085; padding: 15px; text-align: center; border-radius: 5px; margin: 20px 0; font-style: italic; }
</style>
"""

# --- 🧠 2. 核心模块 ---

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
            return item.get('DOI')
    except Exception as e:
        print(f"    ❌ DOI 搜索失败: {e}")
    return None

def extract_body(msg):
    """提取纯文本和所有 http 链接"""
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
                
                raw_urls = find_urls_in_text(part_text)
                extracted_urls.update(raw_urls)
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
    
    # 1. 检测 ArXiv
    for match in re.finditer(r"(?:arXiv:|arxiv\.org/abs/|arxiv\.org/pdf/)\s*(\d{4}\.\d{4,5})", text, re.IGNORECASE):
        aid = match.group(1)
        if aid not in seen_ids:
            results.append({"type": "arxiv", "id": aid, "url": f"https://arxiv.org/pdf/{aid}.pdf"})
            seen_ids.add(aid)
    
    # 2. 检测 DOI
    for match in re.finditer(r"(?:doi:|doi\.org/)\s*(10\.\d{4,9}/[-._;()/:A-Z0-9]+)", text, re.IGNORECASE):
        doi = match.group(1)
        if doi not in seen_ids:
            oa_url = get_oa_link_from_doi(doi)
            results.append({"type": "doi", "id": doi, "url": oa_url})
            seen_ids.add(doi)
    
    # 3. 增强版链接匹配
    ACADEMIC_DOMAINS = [
        'emerald.com', 'researchgate.net', 'wiley.com', 'sciencedirect.com', 
        'springer.com', 'tandfonline.com', 'sagepub.com', 'jstor.org', 'oup.com', 
        'cambridge.org', 'egrove.olemiss.edu'
    ]
    
    BLOCKED_DOMAINS = ['muse.jhu.edu', 'sciencedirect.com/science/article/pii']
    
    if all_links:
        for link in all_links:
            try:
                link = unquote(link)
                link_lower = link.lower()

                if any(x in link_lower for x in ['unsubscribe', 'privacy', 'manage', 'twitter', 'facebook']):
                    continue
                
                if any(blk in link_lower for blk in BLOCKED_DOMAINS):
                    continue

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
                        results.append({
                            "type": source_type,
                            "id": f"link_{link_hash}",
                            "url": link
                        })
                        seen_ids.add(link_hash)
            except: continue
    
    return results

def polite_wait(url):
    try:
        if not url: return
        domain = urlparse(url).netloc
        last_time = DOMAIN_LAST_ACCESSED.get(domain, 0)
        cooldown = 5 + random.uniform(1, 3)
        if time.time() - last_time < cooldown:
            time.sleep(cooldown)
        DOMAIN_LAST_ACCESSED[domain] = time.time()
    except: pass

def fetch_content(source_data, save_dir=None):
    if source_data.get("type") == "arxiv":
        time.sleep(3)

    url = source_data.get("url")
    if not url: 
        if source_data.get("type") == "doi": return fetch_abstract_only(source_data)
        return None, "No URL", None

    polite_wait(url)
    print(f"    🔍 [探测] 正在访问: {url[:50]}...")
    
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8"
    }

    try:
        r = requests.get(url, headers=headers, timeout=30, allow_redirects=True, stream=True)
        
        if r.status_code == 429:
            print("    🛑 [429] 请求过多，冷却 60秒...")
            time.sleep(60)
            return None, "Rate Limited", None
            
        final_url = r.url
        content_type = r.headers.get('Content-Type', '').lower()
        
        is_pdf_response = (
            'application/pdf' in content_type or 
            final_url.endswith('.pdf') or 
            'viewcontent.cgi' in final_url
        )

        if is_pdf_response:
            print("    📥 确认 PDF 内容，开始下载...")
            file_id = source_data.get('id') or hashlib.md5(url.encode()).hexdigest()[:10]
            safe_name = re.sub(r'[\\/*?:"<>|]', '_', file_id)
            filename = os.path.join(save_dir, f"{safe_name}.pdf")
            
            with open(filename, "wb") as f:
                for chunk in r.iter_content(chunk_size=8192):
                    f.write(chunk)
            
            # 🟢 [新增过滤] 检查文件大小，过滤“假 PDF”
            file_size = os.path.getsize(filename)
            if file_size < 2000: # 小于 2KB 的通常是错误页面
                print(f"    ⚠️ 文件过小 ({file_size} bytes)，疑似无效网页/反爬拦截，跳过。")
                os.remove(filename)
                return None, "Fake PDF", None

            try:
                content = pymupdf4llm.to_markdown(filename)
                print(f"    ✅ PDF 解析成功，长度: {len(content)}")
                return content, "PDF Full Text", filename
            except: 
                return None, "PDF Error", None

        elif 'text/html' in content_type:
            print("    🌐 检测到网页，尝试提取正文...")
            html_content = ""
            for chunk in r.iter_content(chunk_size=8192):
                html_content += chunk.decode(errors='ignore')
                if len(html_content) > 200000: break 
            
            text_content = re.sub(r'<script.*?>.*?</script>', '', html_content, flags=re.DOTALL)
            text_content = re.sub(r'<style.*?>.*?</style>', '', text_content, flags=re.DOTALL)
            text_content = re.sub(r'<[^<]+?>', '\n', text_content)
            text_content = re.sub(r'\n+', '\n', text_content).strip()
            
            # 网页内容太短也过滤
            if len(text_content) < 500:
                print(f"    ⚠️ 网页内容过短 ({len(text_content)} chars)，跳过。")
                return None, "Content Too Short", None
            
            print(f"    ✅ 网页文本提取成功，长度: {len(text_content)}")
            return text_content, "Web Page Text", None

    except Exception as e:
        print(f"    ⚠️ 下载失败: {e}")
        if source_data.get("type") == "doi": return fetch_abstract_only(source_data)

    if source_data.get("type") == "doi": return fetch_abstract_only(source_data)
    return None, "Unknown", None

def fetch_abstract_only(source_data):
    try:
        print(f"    📚 [保底] 正在从 Crossref 获取摘要...")
        work = cr.works(ids=source_data["id"])
        title = work['message'].get('title', [''])[0]
        abstract = re.sub(r'<[^>]+>', '', work['message'].get('abstract', '无摘要'))
        content = f"# {title}\n\n## Abstract\n{abstract}"
        return content, "Abstract Only", None
    except: return None, "Error", None

def analyze_with_llm(content, content_type, source_url=""):
    prompt = f"""请深度分析以下文献。来源：{content_type}。在解释机制时插入 

[Image of X]
 标签。输出 Markdown。\n---\n{content[:50000]}"""
    try:
        completion = client.chat.completions.create(
            model=LLM_MODEL_NAME,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.3
        )
        return completion.choices[0].message.content.strip()
    except Exception as e:
        return f"LLM 分析出错: {e}"

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

def send_email_with_attachment(subject, body_markdown, attachment_zip=None):
    try:
        html_content = markdown.markdown(body_markdown, extensions=['extra', 'tables', 'fenced_code'])
    except:
        html_content = body_markdown
    
    # 🟢 修复了你贴的代码里坏掉的正则
    try:
        def replacer(match):
            return f'<div class="image-placeholder">🖼️ 图示建议：{match.group(1)}</div>'
        html_content = re.sub(r'\]+)\]', replacer, html_content)
    except Exception as e:
        print(f"⚠️ 美化失败，使用原始格式: {e}")
        pass
    
    final_html = f"<!DOCTYPE html><html><head><meta charset='UTF-8'>{EMAIL_CSS}</head><body>{html_content}<hr><p style='text-align:center;color:#888;font-size:12px;'>Generated by AI Research Assistant | {datetime.date.today()}</p></body></html>"
    msg = MIMEMultipart()
    msg["Subject"] = subject
    msg["From"] = EMAIL_USER
    msg["To"] = EMAIL_USER
    msg.attach(MIMEText(final_html, "html", "utf-8"))
    
    if attachment_zip and os.path.exists(attachment_zip):
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

# --- 🚀 4. 主逻辑 ---

def main():
    print("🎬 程序启动中...")
    if os.path.exists(DOWNLOAD_DIR): shutil.rmtree(DOWNLOAD_DIR)
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)
    
    processed_ids = load_history()
    mail = imaplib.IMAP4_SSL(IMAP_SERVER)
    mail.login(EMAIL_USER, EMAIL_PASS)
    mail.select("inbox")
    
    date_str = (datetime.date.today() - datetime.timedelta(days=1)).strftime("%d-%b-%Y")
    _, data = mail.search(None, f'(SINCE "{date_str}")')
    email_list = data[0].split()
    print(f"📨 检索到 {len(email_list)} 封邮件")
    
    pending_sources = []
    processed_count = 0
    
    for idx, e_id in enumerate(email_list):
        try:
            _, header_data = mail.fetch(e_id, "(BODY.PEEK[HEADER])")
            msg_header = email.message_from_bytes(header_data[0][1])
            subj, enc = decode_header(msg_header["Subject"])[0]
            subj = subj.decode(enc or 'utf-8') if isinstance(subj, bytes) else subj
            
            if not any(k.lower() in subj.lower() for k in TARGET_SUBJECTS):
                continue
            
            print(f"🎯 命中: {subj[:30]}...")
            _, m_data = mail.fetch(e_id, "(RFC822)")
            msg = email.message_from_bytes(m_data[0][1])
            
            body_text, all_urls = extract_body(msg)
            print(f"    📎 扫描到 {len(all_urls)} 个链接")
            
            sources = detect_and_extract_all(body_text, all_urls)
            
            if not sources:
                print("    💡 无直接链接，尝试 LLM 标题提取...")
                titles = extract_titles_from_text(body_text)
                for t in titles:
                    found_doi = search_doi_by_title(t)
                    if found_doi:
                        print(f"    ✅ 反查 DOI: {found_doi}")
                        oa_url = get_oa_link_from_doi(found_doi)
                        sources.append({"type": "doi", "id": found_doi, "url": oa_url})
                        time.sleep(1)

            for s in sources:
                if get_unique_id(s) not in processed_ids:
                    pending_sources.append(s)
            
            processed_count += 1
            if processed_count % 10 == 0:
                print("🛑 批次休息 5秒...")
                time.sleep(5)
                
        except Exception as e:
            print(f"⚠️ 错误: {e}")
            continue

    MAX_PAPERS = 15
    to_process = pending_sources[:MAX_PAPERS]
    
    if not to_process:
        print("☕ 无新文献。")
        return

    print(f"📑 准备分析 {len(to_process)} 篇文献...")
    report_body, all_files, total_new, failed = "", [], 0, []
    
    for src in to_process:
        print(f"📝 处理: {src.get('id', 'Doc')}")
        content, ctype, path = fetch_content(src, save_dir=DOWNLOAD_DIR)
        
        if path: all_files.append(path)
        
        if content:
            print("🤖 AI 分析中...")
            ans = analyze_with_llm(content, ctype, src.get('url'))
            if "LLM 分析出错" not in ans:
                report_body += f"## 📑 {src.get('id', 'Paper')}\n\n{ans}\n\n---\n\n"
                processed_ids.append(get_unique_id(src))
                total_new += 1
                continue
        failed.append(src)
    
    final_report = f"# 📅 文献日报 {datetime.date.today()}\n\n" + report_body
    if total_new > 0 or failed:
        print("📨 发送邮件...")
        zip_file = "papers.zip" if all_files else None
        if zip_file:
            with zipfile.ZipFile(zip_file, 'w') as zf:
                for f in all_files: zf.write(f, os.path.basename(f))
        
        send_email_with_attachment(f"🤖 AI 学术日报 (新:{total_new})", final_report, zip_file)
        if zip_file and os.path.exists(zip_file): os.remove(zip_file)
    
    save_history(processed_ids)
    print("💾 历史记录已保存。")

if __name__ == "__main__":
    main()
