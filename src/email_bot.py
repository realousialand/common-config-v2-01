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

from urllib.parse import unquote

import markdown



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

    "Imperial history", "Causal inference", "new results"

]



HISTORY_FILE = "data/history.json"

DOWNLOAD_DIR = "downloads"

MAX_ATTACHMENT_SIZE = 19 * 1024 * 1024

socket.setdefaulttimeout(30)



client = OpenAI(api_key=LLM_API_KEY, base_url=LLM_BASE_URL)

cr = Crossref()



# --- 🎨 邮件样式美化 (CSS) ---

EMAIL_CSS = """

<style>

    body { font-family: 'Helvetica Neue', Helvetica, Arial, sans-serif; line-height: 1.6; color: #333; max-width: 800px; margin: 0 auto; padding: 20px; }

    h1 { color: #2c3e50; border-bottom: 2px solid #3498db; padding-bottom: 10px; font-size: 24px; }

    h2 { color: #e67e22; margin-top: 30px; font-size: 20px; border-left: 5px solid #e67e22; padding-left: 10px; background-color: #fdf2e9; }

    h3 { color: #34495e; font-size: 18px; margin-top: 25px; }

    p { margin-bottom: 15px; text-align: justify; }

    strong { color: #c0392b; font-weight: 700; }

    blockquote { border-left: 4px solid #bdc3c7; margin: 0; padding-left: 15px; color: #7f8c8d; background-color: #f9f9f9; padding: 10px; }

    li { margin-bottom: 8px; }

    hr { border: 0; height: 1px; background: #eee; margin: 30px 0; }

    code { background-color: #f4f4f4; padding: 2px 5px; border-radius: 3px; font-family: Monaco, monospace; font-size: 0.9em; color: #e74c3c; }

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

    except: 

        pass

    return None



def extract_body(msg):

    """🟢 提取邮件正文（纯文本 + HTML中的链接）"""

    body_text = ""

    html_links = []

    

    if msg.is_multipart():

        for part in msg.walk():

            content_type = part.get_content_type()

            disposition = str(part.get("Content-Disposition"))

            

            # 提取纯文本

            if content_type == "text/plain" and "attachment" not in disposition:

                try:

                    body_text += part.get_payload(decode=True).decode(errors='ignore') + "\n"

                except:

                    pass

            

            # 🟢 提取 HTML 中的所有链接

            elif content_type == "text/html" and "attachment" not in disposition:

                try:

                    html_content = part.get_payload(decode=True).decode(errors='ignore')

                    # 提取所有 href 链接

                    found_links = re.findall(r'href=["\']([^"\']+)["\']', html_content, re.IGNORECASE)

                    html_links.extend(found_links)

                except:

                    pass

    else:

        try:

            body_text += msg.get_payload(decode=True).decode(errors='ignore')

        except:

            pass

    

    return body_text, html_links



def detect_and_extract_all(text, html_links=None):

    """🟢 检测文献标识符 + HTML链接中的PDF"""

    results = []

    seen_ids = set()

    

    # 1. 检测 ArXiv ID

    for match in re.finditer(r"(?:arXiv:|arxiv\.org/abs/|arxiv\.org/pdf/)\s*(\d{4}\.\d{4,5})", text, re.IGNORECASE):

        aid = match.group(1)

        if aid not in seen_ids:

            results.append({

                "type": "arxiv", 

                "id": aid, 

                "url": f"https://arxiv.org/pdf/{aid}.pdf"

            })

            seen_ids.add(aid)

    

    # 2. 检测 DOI

    for match in re.finditer(r"(?:doi:|doi\.org/)\s*(10\.\d{4,9}/[-._;()/:A-Z0-9]+)", text, re.IGNORECASE):

        doi = match.group(1)

        if doi not in seen_ids:

            oa_url = get_oa_link_from_doi(doi)

            results.append({

                "type": "doi", 

                "id": doi, 

                "url": oa_url

            })

            seen_ids.add(doi)

    

    # 🟢 3. 处理 HTML 链接中的 PDF

    if html_links:

        for link in html_links:

            try:

                # Google Scholar 特殊格式: scholar_url?url=实际链接

                if "scholar_url?url=" in link:

                    actual_url = re.search(r'url=([^&]+)', link)

                    if actual_url:

                        pdf_url = unquote(actual_url.group(1))

                        if pdf_url.endswith('.pdf') or '/pdf/' in pdf_url.lower():

                            link_hash = hashlib.md5(pdf_url.encode()).hexdigest()[:10]

                            if link_hash not in seen_ids:

                                results.append({

                                    "type": "scholar_pdf",

                                    "id": f"gs_{link_hash}",

                                    "url": pdf_url

                                })

                                seen_ids.add(link_hash)

                

                # 直接 PDF 链接

                elif link.endswith('.pdf') or '/pdf/' in link.lower():

                    # 过滤掉一些无效链接

                    if not any(skip in link.lower() for skip in ['unsubscribe', 'privacy', 'terms']):

                        link_hash = hashlib.md5(link.encode()).hexdigest()[:10]

                        if link_hash not in seen_ids:

                            results.append({

                                "type": "direct_pdf",

                                "id": f"pdf_{link_hash}",

                                "url": link

                            })

                            seen_ids.add(link_hash)

            except:

                continue

    

    return results



def fetch_content(source_data, save_dir=None):

    if source_data.get("type") == "arxiv":

        print(f"    ⏳ [ArXiv] 请求频率保护，等待 5s...")

        time.sleep(5)



    if source_data.get("url") and source_data["url"]:

        # 🟢 支持更多 PDF 链接格式

        is_pdf = (

            source_data["url"].endswith(".pdf") or 

            '/pdf/' in source_data["url"].lower() or

            source_data.get("type") in ["arxiv", "scholar_pdf", "direct_pdf"]

        )

        

        if is_pdf:

            print(f"    📥 [下载] 正在抓取 PDF: {source_data['url'][:60]}...")

            try:

                headers = {

                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",

                    "Accept": "application/pdf,*/*"

                }

                r = requests.get(source_data["url"], headers=headers, timeout=45, allow_redirects=True)

                

                if r.status_code == 200 and len(r.content) > 1000:  # 确保不是空文件

                    file_id = source_data.get('id') or hashlib.md5(source_data['url'].encode()).hexdigest()[:10]

                    safe_name = re.sub(r'[\\/*?:"<>|]', '_', file_id)

                    filename = os.path.join(save_dir, f"{safe_name}.pdf") if save_dir else f"temp_{safe_name}.pdf"

                    

                    with open(filename, "wb") as f: 

                        f.write(r.content)

                    

                    content = pymupdf4llm.to_markdown(filename)

                    print(f"    ✅ [成功] 已提取 {len(content)} 字符")

                    return content, "PDF Full Text", filename

                else:

                    print(f"    ⚠️ PDF下载失败: HTTP {r.status_code}, 大小 {len(r.content)} bytes")

            except Exception as e:

                print(f"    ⚠️ 下载中断: {e}")



    # 对于 DOI，尝试获取摘要

    if source_data.get("type") == "doi":

        try:

            print(f"    📚 [Crossref] 正在获取摘要...")

            work = cr.works(ids=source_data["id"])

            title = work['message'].get('title', [''])[0]

            abstract = re.sub(r'<[^>]+>', '', work['message'].get('abstract', '无摘要'))

            content = f"# {title}\n\n## Abstract\n{abstract}"

            return content, "Abstract Only", None

        except Exception as e:

            print(f"    ⚠️ Crossref 查询失败: {e}")

    

    return None, "Unknown", None



def analyze_with_llm(content, content_type, source_url=""):

    prompt = f"""请深度分析以下文献。来源：{content_type}。在解释机制时插入 [Image of X] 标签。输出 Markdown。\n---\n{content[:50000]}"""

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

    return source_data.get("id") or hashlib.md5(source_data.get("url", "").encode()).hexdigest()



def send_email_with_attachment(subject, body_markdown, attachment_zip=None):

    try:

        html_content = markdown.markdown(body_markdown, extensions=['extra', 'tables', 'fenced_code'])

    except Exception as e:

        print(f"Markdown 转换失败: {e}")

        html_content = body_markdown

    

    pattern = r"\[Image of ([^\]]+)\]"

    replacement = r'<div class="image-placeholder">🖼️ 图示建议：\1</div>'

    html_content = re.sub(pattern, replacement, html_content)

    

    final_html = f"""

<!DOCTYPE html>

<html>

<head>

    <meta charset="UTF-8">

    {EMAIL_CSS}

</head>

<body>

    {html_content}

    <footer>

        🤖 Generated by AI Research Assistant | 📅 {datetime.date.today()}

    </footer>

</body>

</html>

"""

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

        except Exception as e:

            print(f"附件挂载失败: {e}")

    

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

    if os.path.exists(DOWNLOAD_DIR):

        shutil.rmtree(DOWNLOAD_DIR)

    os.makedirs(DOWNLOAD_DIR, exist_ok=True)

    

    processed_ids = load_history()

    print(f"📧 正在尝试连接 IMAP 服务器: {IMAP_SERVER}...")

    

    max_retries = 3

    for attempt in range(max_retries):

        try:

            mail = imaplib.IMAP4_SSL(IMAP_SERVER)

            print(f"🔑 正在登录账户: {EMAIL_USER}...")

            mail.login(EMAIL_USER, EMAIL_PASS)

            break

        except Exception as e:

            if attempt < max_retries - 1:

                wait_time = (attempt + 1) * 5

                print(f"⚠️  连接失败，{wait_time}秒后重试...")

                time.sleep(wait_time)

            else:

                raise e

    

    print("📂 已成功登录，正在打开收件箱...")

    mail.select("inbox")

    

    date_str = (datetime.date.today() - datetime.timedelta(days=1)).strftime("%d-%b-%Y")

    print(f"🔍 正在检索 {date_str} 之后的邮件...")

    _, data = mail.search(None, f'(SINCE "{date_str}")')

    

    pending_sources = []

    email_list = data[0].split()

    print(f"📨 检索到共 {len(email_list)} 封近期邮件，开始解析关键词...")

    

    processed_count = 0

    failed_count = 0

    MAX_FAILURES = 5

    DELAY_BETWEEN_EMAILS = 1.5

    DELAY_AFTER_BATCH = 5

    BATCH_SIZE = 10

    OVERQUOTA_COOLDOWN = 30

    

    for idx, e_id in enumerate(email_list, 1):

        try:

            if processed_count > 0:

                print(f"⏸️  等待 {DELAY_BETWEEN_EMAILS} 秒... ({processed_count}/{len(email_list)})")

                time.sleep(DELAY_BETWEEN_EMAILS)

            

            if processed_count > 0 and processed_count % BATCH_SIZE == 0:

                print(f"🛑 已处理 {processed_count} 封，休息 {DELAY_AFTER_BATCH} 秒避免触发限制...")

                time.sleep(DELAY_AFTER_BATCH)

            

            _, header_data = mail.fetch(e_id, "(BODY.PEEK[HEADER])")

            msg_header = email.message_from_bytes(header_data[0][1])

            

            subj, enc = decode_header(msg_header["Subject"])[0]

            subj = subj.decode(enc or 'utf-8') if isinstance(subj, bytes) else subj

            

            if not any(k.lower() in subj.lower() for k in TARGET_SUBJECTS):

                processed_count += 1

                continue

            

            print(f"🎯 命中关键词邮件: {subj[:30]}...")

            

            time.sleep(1)

            _, m_data = mail.fetch(e_id, "(RFC822)")

            msg = email.message_from_bytes(m_data[0][1])

            

            # 🟢 提取文本和HTML链接

            body_text, html_links = extract_body(msg)

            print(f"    📎 找到 {len(html_links)} 个链接")

            

            # 🟢 传入链接列表

            sources = detect_and_extract_all(body_text, html_links)

            print(f"    ✅ 识别出 {len(sources)} 个文献源")

            

            for s in sources:

                if get_unique_id(s) not in processed_ids:

                    pending_sources.append(s)

            

            processed_count += 1

            failed_count = 0

            

        except Exception as e:

            error_msg = str(e)

            print(f"⚠️  解析邮件 {e_id} 时出错: {error_msg}")

            

            if "OVERQUOTA" in error_msg or "exceeded" in error_msg.lower():

                failed_count += 1

                print(f"❌ 触发 Gmail 配额限制！({failed_count}/{MAX_FAILURES})")

                

                if failed_count >= MAX_FAILURES:

                    print(f"🛑 连续失败 {MAX_FAILURES} 次，停止本次运行")

                    print(f"✅ 已成功处理 {processed_count} 封邮件")

                    break

                

                print(f"⏰ 等待 {OVERQUOTA_COOLDOWN} 秒后继续...")

                time.sleep(OVERQUOTA_COOLDOWN)

                

                try:

                    mail.close()

                    mail.logout()

                    time.sleep(5)

                    mail = imaplib.IMAP4_SSL(IMAP_SERVER)

                    mail.login(EMAIL_USER, EMAIL_PASS)

                    mail.select("inbox")

                    print("✅ 重新连接成功")

                except:

                    print("❌ 重新连接失败，停止运行")

                    break

            else:

                failed_count += 1

                if failed_count >= MAX_FAILURES:

                    print(f"🛑 其他错误导致连续失败 {MAX_FAILURES} 次，停止运行")

                    break

            

            continue

    

    try:

        mail.close()

        mail.logout()

    except:

        pass

    

    MAX_PAPERS = 15

    to_process = pending_sources[:MAX_PAPERS]

    if not to_process:

        print("☕ 暂无待处理的新文献，任务结束。")

        return

    

    print(f"📑 队列已就绪: 今日将分析 {len(to_process)} 篇新文献。")

    report_body, all_files, total_new, failed = "", [], 0, []

    

    for src in to_process:

        print(f"📝 正在处理第 {total_new + len(failed) + 1} 篇: {src.get('id', 'Document')}")

        content, ctype, path = fetch_content(src, save_dir=DOWNLOAD_DIR)

        if path:

            all_files.append(path)

        if content:

            print("🤖 正在调用 LLM 进行学术分析...")

            ans = analyze_with_llm(content, ctype, src.get('url'))

            if "LLM 分析出错" not in ans:

                report_body += f"## 📑 {src.get('id', 'Paper')}\n\n{ans}\n\n---\n\n"

                processed_ids.append(get_unique_id(src))

                total_new += 1

                continue

        failed.append(src)

    

    print(f"📊 分析阶段结束。成功: {total_new}, 失败: {len(failed)}")

    

    final_report = f"# 📅 文献日报 {datetime.date.today()}\n\n" + report_body

    if total_new > 0 or failed:

        print("📨 正在打包并发送邮件...")

        zip_file = "papers.zip" if all_files else None

        if zip_file:

            with zipfile.ZipFile(zip_file, 'w') as zf:

                for f in all_files:

                    zf.write(f, os.path.basename(f))

        

        if send_email_with_attachment(f"🤖 AI 学术日报 (新:{total_new})", final_report, zip_file):

            print("📧 邮件发送成功！")

        else:

            print("❌ 邮件发送失败。")

        

        if zip_file and os.path.exists(zip_file):

            os.remove(zip_file)

    

    save_history(processed_ids)

    print("💾 历史记录已保存。")



if __name__ == "__main__":

    main()
