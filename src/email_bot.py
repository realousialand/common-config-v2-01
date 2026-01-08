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

# --- 🛠️ 1. 核心配置区 (环境变量优先) ---
# 这里的配置直接写死或读取环境变量，不再依赖外部文件
LLM_API_KEY = os.environ.get("LLM_API_KEY")  # 必填
LLM_BASE_URL = "https://api.siliconflow.cn/v1"
LLM_MODEL_NAME = os.environ.get("LLM_MODEL_NAME", "deepseek-ai/DeepSeek-R1-distill-llama-70b")

EMAIL_USER = os.environ.get("EMAIL_USER")     # 必填
EMAIL_PASS = os.environ.get("EMAIL_PASS")     # 必填
IMAP_SERVER = "imap.gmail.com"
SMTP_SERVER = "smtp.gmail.com"

# 监控关键词 (根据你的博士研究方向定制)
TARGET_SUBJECTS = [
    "文献鸟", "Google Scholar Alert", "ArXiv", "Project MUSE", 
    "new research", "Stork", "ScienceDirect", "Chinese politics", 
    "Imperial history", "Causal inference"
]

# 本地文件路径配置
HISTORY_FILE = "data/history.json"
DOWNLOAD_DIR = "downloads"
MAX_ATTACHMENT_SIZE = 19 * 1024 * 1024  # 19MB
socket.setdefaulttimeout(60)

# 初始化客户端
client = OpenAI(api_key=LLM_API_KEY, base_url=LLM_BASE_URL)
cr = Crossref()

# --- 🧠 2. LLM 分析核心模块 ---

def get_oa_link_from_doi(doi):
    """利用 Unpaywall API 查找 DOI 是否有免费 PDF"""
    try:
        email_addr = "bot@example.com"
        r = requests.get(f"https://api.unpaywall.org/v2/{doi}?email={email_addr}", timeout=10)
        data = r.json()
        if data.get('is_oa') and data.get('best_oa_location'):
            return data['best_oa_location']['url_for_pdf']
    except:
        pass
    return None

def detect_and_extract_all(text):
    """从文本中提取 ArXiv ID, DOI 和 PDF 链接"""
    results = []
    seen_ids = set() 

    # 1. ArXiv
    for match in re.finditer(r"(?:arXiv ID:|arxiv\.org/abs/)\s*(\d+\.\d+)", text, re.IGNORECASE):
        aid = match.group(1)
        if aid not in seen_ids:
            results.append({"type": "arxiv", "id": aid, "url": f"https://arxiv.org/pdf/{aid}.pdf"})
            seen_ids.add(aid)

    # 2. DOI
    for match in re.finditer(r"doi:\s*(10\.\d{4,9}/[-._;()/:A-Z0-9]+)", text, re.IGNORECASE):
        doi = match.group(1)
        if doi not in seen_ids:
            oa_url = get_oa_link_from_doi(doi)
            results.append({"type": "doi", "id": doi, "url": oa_url})
            seen_ids.add(doi)

    # 3. Direct PDF Links
    for match in re.finditer(r'(https?://[^\s]+\.pdf)', text, re.IGNORECASE):
        url = match.group(1)
        if any(x in url for x in seen_ids): continue
        url_hash = hashlib.md5(url.encode()).hexdigest()
        if url_hash not in seen_ids:
            results.append({"type": "direct_pdf", "id": None, "url": url})
            seen_ids.add(url_hash)

    return results

def fetch_content(source_data, save_dir=None):
    """下载 PDF 或获取 DOI 摘要"""
    # A. PDF 下载
    if source_data.get("url") and source_data["url"].endswith(".pdf"):
        print(f"    📥 [下载中] {source_data['url']}")
        time.sleep(2) 
        try:
            headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
            r = requests.get(source_data["url"], headers=headers, timeout=60)
            if r.status_code == 200:
                # 修复后的 ID 生成逻辑，避免之前的 SyntaxError
                file_id = source_data.get('id')
                if not file_id:
                    file_id = hashlib.md5(source_data['url'].encode()).hexdigest()
                
                safe_name = re.sub(r'[\\/*?:"<>|]', '_', file_id)
                filename = os.path.join(save_dir, f"{safe_name}.pdf") if save_dir else f"temp_{safe_name}.pdf"

                with open(filename, "wb") as f:
                    f.write(r.content)
                
                # 使用 pymupdf4llm 提取为 Markdown
                content = pymupdf4llm.to_markdown(filename)
                return content, "PDF Full Text", filename
        except Exception as e:
            print(f"    ⚠️ PDF 下载失败: {e}")

    # B. DOI 摘要 (备选)
    if source_data["type"] == "doi":
        print(f"    ℹ️ [元数据] 尝试抓取 DOI 摘要: {source_data['id']}")
        try:
            work = cr.works(ids=source_data["id"])
            title = work['message'].get('title', [''])[0]
            abstract = work['message'].get('abstract', '无摘要信息')
            abstract = re.sub(r'<[^>]+>', '', abstract)
            content = f"# {title}\n\n## Abstract\n{abstract}"
            return content, "Abstract Only", None
        except:
            pass
            
    return None, "Unknown", None

def analyze_with_llm(content, content_type, source_url=""):
    """
    LLM 分析函数 - 包含视觉增强指令
    """
    prompt = f"""
    请作为我的学术助手（侧重社会科学与定量研究），基于以下提供的文献内容执行深度分析。
    【文献内容来源】：{content_type}
    【已知链接】：{source_url}

    ### 🎨 视觉增强指令 (Visual Enhancement):
    为了帮助读者直观理解，请在描述**复杂系统架构、算法流程、因果机制、关键数据趋势**或**抽象概念**时，在段落后插入 1-2 个图片搜索标签。
    - **格式**：`

[Image of X]
`
    - **要求**：X 必须是具体、准确的搜索关键词（英文为佳）。
    - **示例**：
      - 提到 Transformer 架构时：`

[Image of Transformer architecture diagram]
`
      - 提到双重差分法趋势时：``
    - **原则**：只在有教育/解释意义时插入。

    ### 📝 任务步骤（请输出 Markdown 格式）：
    1. **基本信息**：标题、作者、年份、期刊。
    2. **研究背景与缺口**：一句话概括。
    3. **核心理论与假设**。
    4. **数据与方法 (重要)**：
       - 数据来源 (Dataset)
       - 核心变量 (IV/DV)
       - 识别策略 (Identification Strategy, 如 IV, DID, RDD 等)
    5. **关键实证结果**：(若文中包含 Markdown 表格，请重点解读显著性系数)
    6. **主要结论与贡献**。
    7. **局限性与未来方向**。
    8. **

[Image of X]
 插入点**：请在正文中自然穿插上述标签。

    ---
    {content[:55000]} 
    ---
    """
    try:
        completion = client.chat.completions.create(
            model=LLM_MODEL_NAME,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.3
        )
        analysis = completion.choices[0].message.content
        analysis = analysis.replace("```markdown", "").replace("```", "").strip()
        return analysis
    except Exception as e:
        return f"LLM 分析出错: {e}"

def simple_translate(text):
    """简单标题翻译"""
    if not text or len(text) < 5: return text
    try:
        completion = client.chat.completions.create(
            model=LLM_MODEL_NAME,
            messages=[
                {"role": "system", "content": "Translate the title to Chinese."},
                {"role": "user", "content": text}
            ],
            temperature=0.3
        )
        return completion.choices[0].message.content.strip()
    except:
        return text

# --- 📧 3. 邮件与附件处理模块 ---

def load_history():
    if os.path.exists(HISTORY_FILE):
        try:
            with open(HISTORY_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except: return []
    return []

def save_history(history_list):
    os.makedirs(os.path.dirname(HISTORY_FILE), exist_ok=True)
    with open(HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(history_list, f, indent=2, ensure_ascii=False)

def get_unique_id(source_data):
    # 这里是修复后的逻辑
    if source_data.get("id"):
        return source_data["id"]
    elif source_data.get("url"):
        return hashlib.md5(source_data["url"].encode()).hexdigest()
    return None

def connect_imap():
    mail = imaplib.IMAP4_SSL(IMAP_SERVER)
    mail.login(EMAIL_USER, EMAIL_PASS)
    return mail

def extract_body(msg):
    """递归提取邮件正文，穿透 .eml"""
    body_text = ""
    if msg.is_multipart():
        for part in msg.walk():
            ctype = part.get_content_type()
            cdispo = str(part.get("Content-Disposition"))
            if ctype == "text/plain" and "attachment" not in cdispo:
                try: body_text += part.get_payload(decode=True).decode(errors='ignore') + "\n"
                except: pass
            elif ctype == "message/rfc822" or (part.get_filename() and part.get_filename().endswith('.eml')):
                try:
                    payload = part.get_payload(0) if isinstance(part.get_payload(), list) else part.get_payload()
                    if isinstance(payload, email.message.Message):
                        body_text += extract_body(payload)
                except: pass
    else:
        try: body_text += msg.get_payload(decode=True).decode(errors='ignore')
        except: pass
    return body_text

def get_emails_from_today():
    try:
        mail = connect_imap()
        mail.select("inbox")
        # 搜索最近 24 小时
        date_str = (datetime.date.today() - datetime.timedelta(days=1)).strftime("%d-%b-%Y")
        status, messages = mail.search(None, f'(SINCE "{date_str}")')
        
        email_ids = messages[0].split()
        target_emails = []
        print(f"🔍 扫描到 {len(email_ids)} 封近期邮件...")
        
        for e_id in email_ids:
            try:
                _, msg_data = mail.fetch(e_id, "(RFC822)")
                for response_part in msg_data:
                    if isinstance(response_part, tuple):
                        msg = email.message_from_bytes(response_part[1])
                        subject, encoding = decode_header(msg["Subject"])[0]
                        if isinstance(subject, bytes):
                            subject = subject.decode(encoding if encoding else "utf-8")
                        
                        if any(k.lower() in subject.lower() for k in TARGET_SUBJECTS):
                            print(f"  ✉️ [命中] {subject[:30]}...")
                            target_emails.append(msg)
            except: continue
        return target_emails
    except Exception as e:
        print(f"❌ 邮箱连接失败: {e}")
        return []

def batch_files_by_size(file_paths, max_size):
    batches = []
    current_batch = []
    current_batch_size = 0
    for f_path in file_paths:
        if not os.path.exists(f_path): continue
        f_size = os.path.getsize(f_path)
        if f_size > max_size:
            batches.append([f_path])
            continue
        if current_batch_size + f_size > max_size:
            batches.append(current_batch)
            current_batch = [f_path]
            current_batch_size = f_size
        else:
            current_batch.append(f_path)
            current_batch_size += f_size
    if current_batch: batches.append(current_batch)
    return batches

def create_zip_for_batch(batch_files, batch_index):
    zip_name = f"papers_part_{batch_index}.zip"
    with zipfile.ZipFile(zip_name, 'w', zipfile.ZIP_DEFLATED) as zf:
        for file in batch_files:
            zf.write(file, os.path.basename(file))
    return zip_name

def send_email_with_attachment(subject, body, attachment_zip=None):
    msg = MIMEMultipart()
    msg["Subject"] = subject
    msg["From"] = EMAIL_USER
    msg["To"] = EMAIL_USER 
    msg.attach(MIMEText(body, "markdown", "utf-8"))

    if attachment_zip and os.path.exists(attachment_zip):
        try:
            with open(attachment_zip, "rb") as f:
                part = MIMEApplication(f.read(), Name=os.path.basename(attachment_zip))
            part['Content-Disposition'] = f'attachment; filename="{os.path.basename(attachment_zip)}"'
            msg.attach(part)
        except Exception as e:
            print(f"❌ 附件挂载失败: {e}")

    try:
        with smtplib.SMTP_SSL(SMTP_SERVER, 465) as server:
            server.login(EMAIL_USER, EMAIL_PASS)
            server.sendmail(EMAIL_USER, EMAIL_USER, msg.as_string())
        print(f"📧 已发送: {subject}")
        return True
    except Exception as e:
        print(f"❌ 发送失败: {e}")
        return False

# --- 🚀 4. 主执行逻辑 ---

def main():
    if os.path.exists(DOWNLOAD_DIR):
        shutil.rmtree(DOWNLOAD_DIR)
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)
    
    processed_ids = load_history()
    emails = get_emails_from_today()
    
    if not emails:
        print("☕ 暂无新的学术推送。")
        return

    report_body = ""
    failed_papers = []
    total_new = 0
    all_files = []
    
    for msg in emails:
        body = extract_body(msg)
        sources = detect_and_extract_all(body)
        
        if not sources: continue
        print(f"  🔎 发现 {len(sources)} 篇文献...")
        
        for src in sources:
            uid = get_unique_id(src)
            if uid in processed_ids:
                print(f"    ⏭️ [跳过] {uid[:10]}")
                continue

            print(f"    🚀 [分析] {src.get('id', 'Document')}")
            content, ctype, saved_path = fetch_content(src, save_dir=DOWNLOAD_DIR)
            
            if saved_path: all_files.append(saved_path)
            
            if content:
                analysis = analyze_with_llm(content, ctype, src.get('url'))
                if "LLM 分析出错" in analysis:
                    failed_papers.append({"id": src.get('id'), "url": src.get('url'), "reason": "AI Error"})
                else:
                    title = src.get('id', 'Paper')
                    report_body += f"## 📑 {title}\n\n{analysis}\n\n---\n\n"
                    processed_ids.append(uid)
                    total_new += 1
                    print(f"    ✅ 完成")
            else:
                failed_papers.append({"id": src.get('id'), "url": src.get('url'), "reason": "Download Failed"})

    # 生成最终报告
    final_report = f"# 📅 文献日报 {datetime.date.today()}\n\n"
    if failed_papers:
        final_report += f"## ⚠️ {len(failed_papers)} 篇处理失败\n"
        for fp in failed_papers:
            zh_title = simple_translate(fp['id'])
            final_report += f"- **{zh_title}**\n  - 原文: {fp['url']}\n  - 原因: {fp['reason']}\n\n"
        final_report += "---\n\n"
    
    if total_new > 0:
        final_report += report_body
    else:
        final_report += "今日无成功分析的文献。\n"

    # 发送
    if total_new > 0 or failed_papers:
        subject = f"🤖 AI 学术日报 (成功:{total_new} 失败:{len(failed_papers)})"
        if not all_files:
            send_email_with_attachment(subject, final_report)
        else:
            batches = batch_files_by_size(all_files, MAX_ATTACHMENT_SIZE)
            total_batches = len(batches)
            for i, batch in enumerate(batches):
                zip_file = create_zip_for_batch(batch, i+1)
                sub_part = f"{subject} (附件 {i+1}/{total_batches})"
                body_part = final_report if i == 0 else "📎 补充附件..."
                send_email_with_attachment(sub_part, body_part, zip_file)
                if os.path.exists(zip_file): os.remove(zip_file)
        
        save_history(processed_ids)
        print("🎉 任务完成！")
    else:
        print("没有需要发送的内容。")

if __name__ == "__main__":
    main()
