import imaplib
import email
from email.header import decode_header
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.application import MIMEApplication
import datetime
import os
import json
import hashlib
import time
import shutil
import zipfile
import socket
# 引入 client 和 MODEL_NAME 用于对失败标题进行简单翻译
from universal_bot import detect_and_extract_all, fetch_content, analyze_with_llm, client, MODEL_NAME

# --- 核心配置区 ---
socket.setdefaulttimeout(60)

EMAIL_USER = os.environ.get("EMAIL_USER")
EMAIL_PASS = os.environ.get("EMAIL_PASS")
IMAP_SERVER = "imap.gmail.com"
SMTP_SERVER = "smtp.gmail.com"
HISTORY_FILE = "data/history.json"
DOWNLOAD_DIR = "downloads"
MAX_ATTACHMENT_SIZE = 19 * 1024 * 1024 

TARGET_SUBJECTS = ["文献鸟", "Google Scholar Alert", "ArXiv", "Project MUSE", "new research", "Stork", "ScienceDirect"]

def load_history():
    if os.path.exists(HISTORY_FILE):
        try:
            with open(HISTORY_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except:
            return []
    return []

def save_history(history_list):
    os.makedirs(os.path.dirname(HISTORY_FILE), exist_ok=True)
    with open(HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(history_list, f, indent=2, ensure_ascii=False)

def get_unique_id(source_data):
    if source_data.get("id"):
        return source_data["id"]
    elif source_data.get("url"):
        return hashlib.md5(source_data["url"].encode()).hexdigest()
    return None

def simple_translate(text):
    """
    专门用于翻译失败文献的标题
    """
    if not text or len(text) < 5: return "无有效标题"
    try:
        completion = client.chat.completions.create(
            model=MODEL_NAME,
            messages=[
                {"role": "system", "content": "你是一个学术翻译助手。请将以下英文标题直译为中文，不要解释。"},
                {"role": "user", "content": text}
            ],
            temperature=0.3
        )
        return completion.choices[0].message.content.strip()
    except:
        return "翻译服务暂不可用"

# --- 邮件与附件处理 ---

def connect_imap():
    mail = imaplib.IMAP4_SSL(IMAP_SERVER)
    mail.login(EMAIL_USER, EMAIL_PASS)
    return mail

def get_emails_from_today():
    try:
        mail = connect_imap()
        mail.select("inbox")
        # 搜索过去 24 小时的邮件
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
                        
                        if any(keyword.lower() in subject.lower() for keyword in TARGET_SUBJECTS):
                            print(f"  ✉️ [命中邮件] *** 标题已隐藏 ***")
                            target_emails.append(msg)
            except Exception as e:
                print(f"  ⚠️ 读取某封邮件出错: {e}")
                continue
        return target_emails
    except Exception as e:
        print(f"❌ IMAP 连接或搜索失败: {e}")
        return []

def extract_body(msg):
    """
    递归提取邮件正文，支持穿透 .eml 附件
    """
    body_text = ""
    if msg.is_multipart():
        for part in msg.walk():
            ctype = part.get_content_type()
            cdispo = str(part.get("Content-Disposition"))

            if ctype == "text/plain" and "attachment" not in cdispo:
                try: body_text += part.get_payload(decode=True).decode(errors='ignore') + "\n"
                except: pass
            elif ctype == "text/html" and "attachment" not in cdispo:
                try: body_text += part.get_payload(decode=True).decode(errors='ignore') + "\n"
                except: pass
            elif ctype == "message/rfc822" or (part.get_filename() and part.get_filename().endswith('.eml')):
                print(f"    📦 发现嵌套邮件附件，正在解包...")
                try:
                    payload = part.get_payload(0) if isinstance(part.get_payload(), list) else part.get_payload()
                    if isinstance(payload, email.message.Message):
                        body_text += "\n--- EML START ---\n" + extract_body(payload) + "\n--- EML END ---\n"
                except: pass
    else:
        try: body_text += msg.get_payload(decode=True).decode(errors='ignore')
        except: pass
    return body_text

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
    print(f"📦 正在打包第 {batch_index} 批附件 ({len(batch_files)} 个文件)...")
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
        print(f"📧 邮件已发送: {subject}")
        return True
    except Exception as e:
        print(f"❌ 发送失败: {e}")
        return False

# --- 主程序 ---

def main():
    if os.path.exists(DOWNLOAD_DIR):
        shutil.rmtree(DOWNLOAD_DIR)
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)
    
    processed_ids = load_history()
    emails = get_emails_from_today()
    
    if not emails:
        print("今天没有相关邮件。")
        return

    # 初始化报告内容
    success_report_body = ""
    failed_papers = [] # 用于存储失败文献的元数据
    
    total_new_count = 0
    all_downloaded_files = []
    
    for msg in emails:
        body = extract_body(msg)
        source_list = detect_and_extract_all(body)
        
        if not source_list: continue
        print(f"    🔎 邮件内发现 {len(source_list)} 篇潜在文献...")
        
        for source_data in source_list:
            unique_id = get_unique_id(source_data)
            if unique_id in processed_ids:
                print(f"    ⏭️ [跳过] 已分析过")
                continue

            print(f"    🚀 [正在分析] ...")
            
            # 尝试获取内容
            content, ctype, saved_path = fetch_content(source_data, save_dir=DOWNLOAD_DIR)
            
            if saved_path:
                all_downloaded_files.append(saved_path)
            
            # --- 分支逻辑：成功 vs 失败 ---
            if content:
                analysis = analyze_with_llm(content, ctype, source_url=source_data.get('url', ''))
                
                # 检查 LLM 是否返回了错误信息
                if analysis.startswith("LLM 分析出错"):
                    print(f"    ⚠️ 分析失败: {unique_id}")
                    failed_papers.append({
                        "id": source_data.get('id', 'Unknown ID'),
                        "url": source_data.get('url', ''),
                        "reason": "Analysis Error (AI分析失败)",
                        "error_msg": analysis
                    })
                else:
                    # 成功
                    paper_title = source_data.get('id', 'Paper')
                    success_report_body += f"## 📑 {paper_title}\n\n{analysis}\n\n---\n\n"
                    processed_ids.append(unique_id)
                    total_new_count += 1
                    print(f"    ✅ 分析完成")
            else:
                # 下载失败 (content is None)
                print(f"    ❌ 下载失败，加入失败列表。")
                failed_papers.append({
                    "id": source_data.get('id', 'Unknown ID'),
                    "url": source_data.get('url', ''),
                    "reason": "Download Failed (下载/抓取失败)",
                    "error_msg": "无法获取 PDF 或摘要元数据"
                })

    # --- 构建最终邮件内容 ---
    
    final_report = "# 📅 今日文献深度分析\n\n"
    
    # 1. 优先展示失败列表 (如果有)
    if failed_papers:
        final_report += f"## ⚠️ 有 {len(failed_papers)} 篇文献处理失败\n"
        final_report += "> 以下文献无法获取全文或分析失败，请手动查阅。\n\n"
        
        for idx, fp in enumerate(failed_papers, 1):
            title = fp['id']
            url = fp['url']
            reason = fp['reason']
            
            # 尝试翻译标题 (Best Effort)
            translated_title = simple_translate(title)
            
            final_report += f"### {idx}. {title}\n"
            final_report += f"- **中文译名**: {translated_title}\n"
            final_report += f"- **原始链接**: [点击跳转]({url})\n"
            final_report += f"- **DOI/ID**: `{title}`\n"
            final_report += f"- **失败原因**: {reason}\n\n"
        
        final_report += "---\n\n"

    # 2. 拼接成功报告
    if total_new_count > 0:
        final_report += success_report_body
    else:
        final_report += "今日没有分析成功的文献。\n"

    # --- 发送逻辑 ---
    if total_new_count > 0 or failed_papers:
        base_subject = f"🤖 AI 文献日报 (成功: {total_new_count} | 失败: {len(failed_papers)}) - {datetime.date.today()}"
        
        if not all_downloaded_files:
            send_email_with_attachment(base_subject, final_report, None)
        else:
            batches = batch_files_by_size(all_downloaded_files, MAX_ATTACHMENT_SIZE)
            total_batches = len(batches)
            
            print(f"📦 共 {len(all_downloaded_files)} 个附件，分 {total_batches} 封发送。")
            
            for i, batch in enumerate(batches):
                batch_num = i + 1
                zip_filename = create_zip_for_batch(batch, batch_num)
                subject_with_part = f"{base_subject} (附件 Part {batch_num}/{total_batches})"
                
                if batch_num == 1:
                    email_body = final_report + f"\n\n> 📎 **附件说明**：共 {total_batches} 封邮件，这是第 {batch_num} 封。"
                else:
                    email_body = f"# 📎 补充附件 (Part {batch_num}/{total_batches})\n\n这是今日文献的后续原文包。"
                
                send_email_with_attachment(subject_with_part, email_body, zip_filename)
                
                if os.path.exists(zip_filename):
                    os.remove(zip_filename)

        save_history(processed_ids)
        print(f"🎉 全部完成！")
    else:
        print("☕ 没有新内容需要发送。")

if __name__ == "__main__":
    main()
