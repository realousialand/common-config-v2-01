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
import socket  # 👈 关键库：用于设置网络超时
from universal_bot import detect_and_extract_all, fetch_content, analyze_with_llm

# --- 核心配置区 ---
# 1. 设置全局网络超时 (60秒)，防止 IMAP 或下载无限卡死
socket.setdefaulttimeout(60)

EMAIL_USER = os.environ.get("EMAIL_USER")
EMAIL_PASS = os.environ.get("EMAIL_PASS")
IMAP_SERVER = "imap.gmail.com"
SMTP_SERVER = "smtp.gmail.com"
HISTORY_FILE = "data/history.json"
DOWNLOAD_DIR = "downloads" # 临时存放下载文件的目录

# 2. 邮件附件安全阈值 (19MB)
# Gmail 限制 25MB，预留 Base64 编码膨胀空间，19MB 是安全线
MAX_ATTACHMENT_SIZE = 19 * 1024 * 1024 

# 邮件白名单
TARGET_SUBJECTS = ["文献鸟", "Google Scholar Alert", "ArXiv", "Project MUSE", "new research", "Stork"]

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

# --- 邮件链接部分 ---

def connect_imap():
    mail = imaplib.IMAP4_SSL(IMAP_SERVER)
    mail.login(EMAIL_USER, EMAIL_PASS)
    return mail

def get_emails_from_today():
    try:
        mail = connect_imap()
        mail.select("inbox")
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
                            # 隐私保护：日志中不打印完整标题
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
    if msg.is_multipart():
        for part in msg.walk():
            ctype = part.get_content_type()
            if ctype == "text/plain" and "attachment" not in str(part.get("Content-Disposition")):
                return part.get_payload(decode=True).decode()
            elif ctype == "text/html": 
                html = part.get_payload(decode=True).decode()
                return html
    else:
        return msg.get_payload(decode=True).decode()
    return ""

# --- 附件打包与分发逻辑 ---

def batch_files_by_size(file_paths, max_size):
    """
    智能分堆算法：将文件列表拆分成多个批次，每批次不超过 max_size
    """
    batches = []
    current_batch = []
    current_batch_size = 0
    
    for f_path in file_paths:
        if not os.path.exists(f_path): continue
        
        f_size = os.path.getsize(f_path)
        
        # 单个文件过大，强制单独一封
        if f_size > max_size:
            print(f"  ⚠️ 文件过大 ({f_size/1024/1024:.2f}MB)，将单独分包: {os.path.basename(f_path)}")
            batches.append([f_path])
            continue
            
        if current_batch_size + f_size > max_size:
            batches.append(current_batch)
            current_batch = [f_path]
            current_batch_size = f_size
        else:
            current_batch.append(f_path)
            current_batch_size += f_size
            
    if current_batch:
        batches.append(current_batch)
        
    return batches

def create_zip_for_batch(batch_files, batch_index):
    """为批次创建 ZIP"""
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
    # 初始化下载目录
    if os.path.exists(DOWNLOAD_DIR):
        shutil.rmtree(DOWNLOAD_DIR)
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)
    
    processed_ids = load_history()
    emails = get_emails_from_today()
    
    if not emails:
        print("今天没有相关邮件。")
        return

    daily_report_body = "# 📅 今日文献深度分析\n\n"
    total_new_count = 0
    all_downloaded_files = []
    
    for msg in emails:
        subject = decode_header(msg["Subject"])[0][0]
        if isinstance(subject, bytes): subject = subject.decode()
        
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
            
            # --- 关键调用：同时获取内容和文件路径 ---
            # 必须配合更新后的 universal_bot.py 使用
            content, ctype, saved_path = fetch_content(source_data, save_dir=DOWNLOAD_DIR)
            
            if saved_path:
                all_downloaded_files.append(saved_path)
            
            if content:
                analysis = analyze_with_llm(content, ctype, source_url=source_data.get('url', ''))
                paper_title = source_data.get('id', 'Paper')
                daily_report_body += f"## 📑 {paper_title}\n\n{analysis}\n\n---\n\n"
                processed_ids.append(unique_id)
                total_new_count += 1
            else:
                print(f"    ❌ 下载/分析失败，跳过。")

    # --- 结果发送逻辑 ---
    if total_new_count > 0:
        base_subject = f"🤖 AI 文献日报 - {datetime.date.today()}"
        
        if not all_downloaded_files:
            # 没有附件，直接发
            send_email_with_attachment(base_subject, daily_report_body, None)
        else:
            
            # 有附件，进行分批逻辑
            batches = batch_files_by_size(all_downloaded_files, MAX_ATTACHMENT_SIZE)
            total_batches = len(batches)
            
            print(f"📦 下载文件总数: {len(all_downloaded_files)}，拆分为 {total_batches} 封邮件发送。")
            
            for i, batch in enumerate(batches):
                batch_num = i + 1
                zip_filename = create_zip_for_batch(batch, batch_num)
                
                subject_with_part = f"{base_subject} (附件 Part {batch_num}/{total_batches})"
                
                # 第一封放正文，后面的只放附件
                if batch_num == 1:
                    email_body = daily_report_body + f"\n\n> 📎 **附件说明**：文献原文已打包。共 {total_batches} 封邮件，这是第 {batch_num} 封。"
                else:
                    email_body = f"# 📎 补充附件 (Part {batch_num}/{total_batches})\n\n这是今日文献的后续原文包，请查收。"
                
                send_email_with_attachment(subject_with_part, email_body, zip_filename)
                
                if os.path.exists(zip_filename):
                    os.remove(zip_filename)

        save_history(processed_ids)
        print(f"🎉 全部完成！共更新 {total_new_count} 篇文献。")
    else:
        print("☕ 所有内容都已分析过。")

if __name__ == "__main__":
    main()
