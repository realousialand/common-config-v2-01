import imaplib
import email
from email.header import decode_header
import smtplib
from email.mime.text import MIMEText
import datetime
import os
import json
import hashlib
import time
import re
import requests
import pymupdf4llm
from openai import OpenAI
from habanero import Crossref
from bs4 import BeautifulSoup

# --- 核心配置区 ---
EMAIL_USER = os.environ.get("EMAIL_USER")
EMAIL_PASS = os.environ.get("EMAIL_PASS")
API_KEY = os.environ.get("LLM_API_KEY")
IMAP_SERVER = "imap.gmail.com"
SMTP_SERVER = "smtp.gmail.com"
HISTORY_FILE = "data/history.json"

# 硅基流动配置
BASE_URL = "https://api.siliconflow.cn/v1"
MODEL_NAME = "deepseek-ai/DeepSeek-R1-0528-Qwen3-8B"

# 邮件白名单
TARGET_SUBJECTS = ["文献鸟", "Google Scholar Alert", "ArXiv", "Project MUSE", "new research", "Stork"]

# 初始化 API 客户端
client = OpenAI(api_key=API_KEY, base_url=BASE_URL)
cr = Crossref()

# --- 辅助函数 ---

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

def get_oa_link_from_doi(doi):
    """利用 Unpaywall API 查找 DOI 是否有免费 PDF"""
    try:
        email = "bot@example.com"
        r = requests.get(f"https://api.unpaywall.org/v2/{doi}?email={email}", timeout=5)
        data = r.json()
        if data.get('is_oa') and data.get('best_oa_location'):
            return data['best_oa_location']['url_for_pdf']
    except:
        pass
    return None

# --- 多目标提取器 ---
def detect_and_extract_all(text):
    results = []
    seen_ids = set() 

    # 1. ArXiv ID
    for match in re.finditer(r"(?:arXiv ID:|arxiv\.org/abs/)\s*(\d+\.\d+)", text, re.IGNORECASE):
        aid = match.group(1)
        if aid not in seen_ids:
            results.append({
                "type": "arxiv",
                "id": aid,
                "url": f"https://arxiv.org/pdf/{aid}.pdf"
            })
            seen_ids.add(aid)

    # 2. DOI
    for match in re.finditer(r"doi:\s*(10\.\d{4,9}/[-._;()/:A-Z0-9]+)", text, re.IGNORECASE):
        doi = match.group(1)
        if doi not in seen_ids:
            oa_url = get_oa_link_from_doi(doi)
            results.append({
                "type": "doi",
                "id": doi,
                "url": oa_url 
            })
            seen_ids.add(doi)

    # 3. Direct PDF
    for match in re.finditer(r'(https?://[^\s]+\.pdf)', text, re.IGNORECASE):
        url = match.group(1)
        if any(x in url for x in seen_ids): continue
        
        url_hash = hashlib.md5(url.encode()).hexdigest()
        if url_hash not in seen_ids:
            results.append({
                "type": "direct_pdf",
                "id": None, 
                "url": url
            })
            seen_ids.add(url_hash)

    return results

def fetch_content(source_data):
    content = ""
    source_type = "Full Text"

    # A. PDF 下载
    if source_data["url"] and source_data["url"].endswith(".pdf"):
        print(f"    📥 [下载] {source_data['url']}")
        time.sleep(3) 
        try:
            headers = {"User-Agent": "Mozilla/5.0"}
            r = requests.get(source_data["url"], headers=headers, timeout=60)
            if r.status_code == 200:
                with open("temp.pdf", "wb") as f:
                    f.write(r.content)
                content = pymupdf4llm.to_markdown("temp.pdf")
                os.remove("temp.pdf")
                return content, "PDF Full Text"
        except Exception as e:
            print(f"    ⚠️ PDF 下载失败: {e}")

    # B. DOI 摘要抓取
    if source_data["type"] == "doi":
        print(f"    ℹ️ [元数据] 尝试抓取摘要 DOI: {source_data['id']}")
        try:
            work = cr.works(ids=source_data["id"])
            title = work['message'].get('title', [''])[0]
            abstract = work['message'].get('abstract', '无摘要信息')
            abstract = re.sub(r'<[^>]+>', '', abstract)
            content = f"# {title}\n\n## Abstract\n{abstract}"
            return content, "Abstract Only"
        except:
            pass
            
    return None, "Unknown"

# --- 核心修改：升级版分析函数 ---
def analyze_with_llm(content, content_type, source_url=""):
    """
    使用用户自定义的高级学术 Prompt 进行分析
    """
    prompt = f"""
    请作为我的学术助手，基于以下提供的文献内容执行任务。
    
    【文献内容来源】：{content_type}
    【已知链接】：{source_url}

    请按以下步骤执行（请输出 Markdown 格式）：

    1. **确认并复述文献基本信息**：
       - 从文中提取并补全：标题、作者、期刊/会议（如缩写请补全）、年份、关键词。
    
    2. **研究领域与影响力推断**：
       - 推断文献的研究领域和可能的影响力。

    3. **研究现状与缺口**：
       - 清晰阐述本研究领域的现状和本文要解决的具体研究缺口或问题。

    4. **关键技术与创新**：
       - 详细说明本文采用的关键技术、实验设计或理论框架, 并明确其创新之处。

    5. **核心结论**：
       - 分点列出最重要的实证结果和研究结论。

    6. **术语解释**：
       - 解释文中可能对非专业读者构成障碍的2-3个专业术语或概念。

    7. **优势与贡献**：
       - 分析本研究的主要优势和对领域的贡献。

    8. **局限性与未来方向**：
       - 批判性地讨论本研究可能存在的局限性(如样本量、方法假设等), 并提出未来可能的研究方向。

    9. **相关文献推荐**：
       - 基于你的知识库，推荐3-5篇与本文献高度相关的基础性文献或后续跟进研究 , 并简要说明关联性。

    10. **学术搜索模拟**：
        - 利用你的知识库模拟学术数据库搜索，列出本文的核心引用网络。

    11. **DOI与链接**：
        - 必须提供完整的DOI号及可直达的DOI链接。
        - 如果无法找到DOI，请提供替代的官方来源链接 (如期刊主页、arXiv链接) ,并解释原因。（参考已知链接：{source_url}）

    12. **量化分析提取**（如果适用）：
        - 如果该论文使用量化方法，请专门列出：**Data/Dataset**、**变量**、**模型**、**统计方法**、**数据来源**、**数据处理方法**和**数据结果**。

    ---
    **以下是文献内容：**
    {content[:50000]} 
    ---
    """
    try:
        completion = client.chat.completions.create(
            model=MODEL_NAME,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.3
        )
        return completion.choices[0].message.content
    except Exception as e:
        return f"LLM 分析出错: {e}"

# --- 邮件处理部分 ---

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
                    
                    if any(keyword.lower() in subject.lower() for keyword in TARGET_SUBJECTS):
                        print(f"  ✉️ [命中邮件] {subject}")
                        target_emails.append(msg)
        except:
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
    msg["Subject"] = f"🤖 AI 文献深度分析日报 (共 {report_content.count('# 1. **确认')} 篇) - {datetime.date.today()}"
    msg["From"] = EMAIL_USER
    msg["To"] = EMAIL_USER 

    with smtplib.SMTP_SSL(SMTP_SERVER, 465) as server:
        server.login(EMAIL_USER, EMAIL_PASS)
        server.sendmail(EMAIL_USER, EMAIL_USER, msg.as_string())
    print("📧 汇总邮件已发送！")

def main():
    processed_ids = load_history()
    emails = get_emails_from_today()
    
    if not emails:
        print("今天没有相关邮件。")
        return

    daily_report = "# 📅 今日文献深度分析\n\n"
    total_new_count = 0
    
    for msg in emails:
        subject = decode_header(msg["Subject"])[0][0]
        if isinstance(subject, bytes): subject = subject.decode()
        
        body = extract_body(msg)
        source_list = detect_and_extract_all(body)
        
        if not source_list:
            continue
            
        print(f"    🔎 邮件内发现 {len(source_list)} 篇潜在文献...")

        for source_data in source_list:
            unique_id = get_unique_id(source_data)
            
            if unique_id in processed_ids:
                print(f"    ⏭️ [跳过] 已分析过: {unique_id}")
                continue

            print(f"    🚀 [分析] ID: {unique_id}")
            content, ctype = fetch_content(source_data)
            
            if content:
                # 传入 source_url 以便 LLM 填写第11点
                analysis = analyze_with_llm(content, ctype, source_url=source_data.get('url', ''))
                
                paper_title = source_data.get('id', 'Paper Analysis')
                daily_report += f"## 📑 文献 ID: {paper_title}\n\n{analysis}\n\n---\n\n"
                processed_ids.append(unique_id)
                total_new_count += 1
            else:
                print(f"    ❌ 下载失败，跳过。")
    
    if total_new_count > 0:
        send_daily_report(daily_report)
        save_history(processed_ids)
        print(f"🎉 全部完成！共更新 {total_new_count} 篇文献。")
    else:
        print("☕ 所有内容都已分析过。")

if __name__ == "__main__":
    main()
