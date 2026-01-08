import imaplib
import email
from email.header import decode_header
import smtplib
from email.mime.text import MIMEText
import datetime
import os
import json
import hashlib
import time # 引入time用于延时
from universal_bot import detect_and_extract, fetch_content, analyze_with_llm

# 配置
EMAIL_USER = os.environ.get("EMAIL_USER")
EMAIL_PASS = os.environ.get("EMAIL_PASS")
IMAP_SERVER = "imap.gmail.com"
SMTP_SERVER = "smtp.gmail.com"
HISTORY_FILE = "data/history.json"

# 只保留白名单，不再使用黑名单过滤标题
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

def connect_imap():
    mail = imaplib.IMAP4_SSL(IMAP_SERVER)
    mail.login(EMAIL_USER, EMAIL_PASS)
    return mail

def get_emails_from_today():
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
                    
                    # 只要标题命中关键词，就放入待处理队列
                    # 具体的垃圾过滤交给后面的提取函数 detect_and_extract 去做
                    if any(keyword.lower() in subject.lower() for keyword in TARGET_SUBJECTS):
                        print(f"  ✅ 命中邮件: {subject}")
                        target_emails.append(msg)

        except Exception as e:
            print(f"  ⚠️ 读取邮件出错: {e}")
            continue
    
    return target_emails

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

def send_daily_report(report_content):
    msg = MIMEText(report_content, "markdown", "utf-8")
    msg["Subject"] = f"🤖 AI 文献日报 - {datetime.date.today()}"
    msg["From"] = EMAIL_USER
    msg["To"] = EMAIL_USER 

    with smtplib.SMTP_SSL(SMTP_SERVER, 465) as server:
        server.login(EMAIL_USER, EMAIL_PASS)
        server.sendmail(EMAIL_USER, EMAIL_USER, msg.as_string())
    print("📧 邮件已发送！")

def main():
    processed_ids = load_history()
    emails = get_emails_from_today()
    
    if not emails:
        print("今天没有相关邮件。")
        return

    daily_report = "# 📅 今日文献深度分析\n\n"
    new_count = 0
    
    for msg in emails:
        subject = decode_header(msg["Subject"])[0][0]
        if isinstance(subject, bytes): subject = subject.decode()
        
        body = extract_body(msg)
        
        # --- 核心逻辑变化 ---
        # 我们把邮件正文扔给提取器。
        # 如果正文里全是“讲座通知”、“教程”，没有 DOI/PDF/ArXiv，
        # detect_and_extract 会直接返回 None，从而自动跳过。
        source_data = detect_and_extract(body)
        
        if not source_data:
            print(f"  🗑️ 未发现有效论文链接，跳过: {subject}")
            continue
            
        unique_id = get_unique_id(source_data)
        if unique_id in processed_ids:
            print(f"⏭️ 已存在历史记录: {unique_id}")
            continue

        print(f"🚀 有效论文，分析中: {subject}")
        
        # 增加延时，保护 IP
        if source_data.get("url") and "arxiv" in source_data["url"]:
             time.sleep(3)

        content, ctype = fetch_content(source_data)
        
        if content:
            analysis = analyze_with_llm(content, ctype)
            daily_report += f"## 📑 {subject}\n**来源**: {source_data['url']}\n\n{analysis}\n\n---\n\n"
            processed_ids.append(unique_id)
            new_count += 1
        else:
            print(f"❌ 下载失败: {subject}")

    if new_count > 0:
        send_daily_report(daily_report)
        save_history(processed_ids)
        print(f"完成！更新了 {new_count} 条记录。")
    else:
        print("没有新内容需要发送。")

if __name__ == "__main__":
    main()
