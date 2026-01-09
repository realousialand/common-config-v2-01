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

# --- 🛠️ 1. Core Configuration ---
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

# --- 🎨 Email CSS ---
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

# --- 🧠 2. Core Modules ---

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

# 🟢 New Function: Extract titles using LLM
def extract_titles_from_text(text):
    """Uses LLM to clean up the email text and extract paper titles."""
    print("    🧠 Using LLM to extract titles from email text...")
    prompt = f"""
    Please extract the full titles of academic papers from the email text below.
    Ignore "Table of Contents", "Obituary", page numbers, or journal names.
    Only return a pure JSON list of strings. Example: ["Title 1", "Title 2"].
    Do not output any Markdown formatting.
    
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
        # Clean potential markdown
        content = content.replace("```json", "").replace("```", "").strip()
        return json.loads(content)
    except Exception as e:
        print(f"    ⚠️ Title extraction failed: {e}")
        return []

# 🟢 New Function: Search DOI by Title
def search_doi_by_title(title):
    """Uses Crossref API to find DOI from a title."""
    print(f"    🔍 Searching DOI for: {title[:30]}...")
    try:
        # Use habanero to search
        results = cr.works(query=title, limit=1)
        if results['message']['items']:
            item = results['message']['items'][0]
            # Basic validation: ensure we got a DOI back
            return item.get('DOI')
    except Exception as e:
        print(f"    ❌ DOI search failed: {e}")
    return None

def extract_body(msg):
    """Extracts email body (Plain Text + HTML Links)"""
    body_text = ""
    html_links = []
    
    if msg.is_multipart():
        for part in msg.walk():
            content_type = part.get_content_type()
            disposition = str(part.get("Content-Disposition"))
            
            # Extract Plain Text
            if content_type == "text/plain" and "attachment" not in disposition:
                try:
                    body_text += part.get_payload(decode=True).decode(errors='ignore') + "\n"
                except:
                    pass
            
            # Extract Links from HTML
            elif content_type == "text/html" and "attachment" not in disposition:
                try:
                    html_content = part.get_payload(decode=True).decode(errors='ignore')
                    # Extract text content from HTML for better LLM parsing
                    clean_text = re.sub('<[^<]+?>', '', html_content)
                    body_text += clean_text + "\n"
                    
                    # Extract all href links
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
    """Detects IDs and HTML PDF links"""
    results = []
    seen_ids = set()
    
    # 1. Detect ArXiv ID
    for match in re.finditer(r"(?:arXiv:|arxiv\.org/abs/|arxiv\.org/pdf/)\s*(\d{4}\.\d{4,5})", text, re.IGNORECASE):
        aid = match.group(1)
        if aid not in seen_ids:
            results.append({
                "type": "arxiv", 
                "id": aid, 
                "url": f"https://arxiv.org/pdf/{aid}.pdf"
            })
            seen_ids.add(aid)
    
    # 2. Detect DOI
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
    
    # 3. Process HTML Links for PDFs
    if html_links:
        for link in html_links:
            try:
                # Google Scholar special format
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
                
                # Direct PDF links
                elif link.endswith('.pdf') or '/pdf/' in link.lower():
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
        print(f"    ⏳ [ArXiv] Rate limit protection, waiting 5s...")
        time.sleep(5)

    if source_data.get("url") and source_data["url"]:
        is_pdf = (
            source_data["url"].endswith(".pdf") or 
            '/pdf/' in source_data["url"].lower() or
            source_data.get("type") in ["arxiv", "scholar_pdf", "direct_pdf"]
        )
        
        if is_pdf:
            print(f"    📥 [Downloading] PDF: {source_data['url'][:60]}...")
            try:
                headers = {
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                    "Accept": "application/pdf,*/*"
                }
                r = requests.get(source_data["url"], headers=headers, timeout=45, allow_redirects=True)
                
                if r.status_code == 200 and len(r.content) > 1000:
                    file_id = source_data.get('id') or hashlib.md5(source_data['url'].encode()).hexdigest()[:10]
                    safe_name = re.sub(r'[\\/*?:"<>|]', '_', file_id)
                    filename = os.path.join(save_dir, f"{safe_name}.pdf") if save_dir else f"temp_{safe_name}.pdf"
                    
                    with open(filename, "wb") as f: 
                        f.write(r.content)
                    
                    content = pymupdf4llm.to_markdown(filename)
                    print(f"    ✅ [Success] Extracted {len(content)} chars")
                    return content, "PDF Full Text", filename
                else:
                    print(f"    ⚠️ PDF Download Failed: HTTP {r.status_code}, Size {len(r.content)} bytes")
            except Exception as e:
                print(f"    ⚠️ Download Error: {e}")

    # For DOIs, try to get Abstract
    if source_data.get("type") == "doi":
        try:
            print(f"    📚 [Crossref] Fetching abstract...")
            work = cr.works(ids=source_data["id"])
            title = work['message'].get('title', [''])[0]
            abstract = re.sub(r'<[^>]+>', '', work['message'].get('abstract', 'No Abstract Available'))
            content = f"# {title}\n\n## Abstract\n{abstract}"
            return content, "Abstract Only", None
        except Exception as e:
            print(f"    ⚠️ Crossref Lookup Failed: {e}")
    
    return None, "Unknown", None

def analyze_with_llm(content, content_type, source_url=""):
    prompt = f"""Please analyze the following academic literature deeply. Source: {content_type}. Insert 

[Image of X]
 tags when explaining mechanisms. Output in Markdown.\n---\n{content[:50000]}"""
    try:
        completion = client.chat.completions.create(
            model=LLM_MODEL_NAME,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.3
        )
        return completion.choices[0].message.content.strip()
    except Exception as e:
        return f"LLM Analysis Error: {e}"

# --- 📧 3. Helper Functions ---

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
        print(f"Markdown conversion failed: {e}")
        html_content = body_markdown
    
    pattern = r"\]+)\]"
    replacement = r'<div class="image-placeholder">🖼️ Diagram Suggestion: \1</div>'
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
            print(f"Attachment failed: {e}")
    
    try:
        with smtplib.SMTP_SSL(SMTP_SERVER, 465) as server:
            server.login(EMAIL_USER, EMAIL_PASS)
            server.sendmail(EMAIL_USER, EMAIL_USER, msg.as_string())
        return True
    except Exception as e:
        print(f"Send failed: {e}")
        return False

# --- 🚀 4. Main Logic ---

def main():
    print("🎬 Program Starting...")
    if os.path.exists(DOWNLOAD_DIR):
        shutil.rmtree(DOWNLOAD_DIR)
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)
    
    processed_ids = load_history()
    print(f"📧 Connecting to IMAP: {IMAP_SERVER}...")
    
    max_retries = 3
    for attempt in range(max_retries):
        try:
            mail = imaplib.IMAP4_SSL(IMAP_SERVER)
            print(f"🔑 Logging in: {EMAIL_USER}...")
            mail.login(EMAIL_USER, EMAIL_PASS)
            break
        except Exception as e:
            if attempt < max_retries - 1:
                wait_time = (attempt + 1) * 5
                print(f"⚠️ Connection failed, retrying in {wait_time}s...")
                time.sleep(wait_time)
            else:
                raise e
    
    print("📂 Logged in, opening Inbox...")
    mail.select("inbox")
    
    date_str = (datetime.date.today() - datetime.timedelta(days=1)).strftime("%d-%b-%Y")
    print(f"🔍 Searching emails since {date_str}...")
    _, data = mail.search(None, f'(SINCE "{date_str}")')
    
    pending_sources = []
    email_list = data[0].split()
    print(f"📨 Found {len(email_list)} recent emails, parsing...")
    
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
                print(f"⏸️ Waiting {DELAY_BETWEEN_EMAILS}s... ({processed_count}/{len(email_list)})")
                time.sleep(DELAY_BETWEEN_EMAILS)
            
            if processed_count > 0 and processed_count % BATCH_SIZE == 0:
                print(f"🛑 Batch limit, resting {DELAY_AFTER_BATCH}s...")
                time.sleep(DELAY_AFTER_BATCH)
            
            _, header_data = mail.fetch(e_id, "(BODY.PEEK[HEADER])")
            msg_header = email.message_from_bytes(header_data[0][1])
            
            subj, enc = decode_header(msg_header["Subject"])[0]
            subj = subj.decode(enc or 'utf-8') if isinstance(subj, bytes) else subj
            
            if not any(k.lower() in subj.lower() for k in TARGET_SUBJECTS):
                processed_count += 1
                continue
            
            print(f"🎯 Hit Target Email: {subj[:30]}...")
            
            time.sleep(1)
            _, m_data = mail.fetch(e_id, "(RFC822)")
            msg = email.message_from_bytes(m_data[0][1])
            
            # Extract text and HTML links
            body_text, html_links = extract_body(msg)
            print(f"    📎 Found {len(html_links)} HTML links")
            
            # 🟢 1. Try standard extraction first
            sources = detect_and_extract_all(body_text, html_links)
            
            # 🟢 2. Fallback: If no sources found and likely a list of papers
            # Checks if no sources found AND text contains indicators of papers
            if not sources and any(k in body_text for k in ["[PDF]", "[HTML]", "Table of Contents"]):
                print("    💡 Standard extraction empty, attempting LLM Title Extraction...")
                titles = extract_titles_from_text(body_text)
                for t in titles:
                    found_doi = search_doi_by_title(t)
                    if found_doi:
                        print(f"    ✅ Found DOI: {found_doi}")
                        oa_url = get_oa_link_from_doi(found_doi)
                        sources.append({"type": "doi", "id": found_doi, "url": oa_url})
                        time.sleep(1)

            print(f"    ✅ Identified {len(sources)} sources")
            
            for s in sources:
                if get_unique_id(s) not in processed_ids:
                    pending_sources.append(s)
            
            processed_count += 1
            failed_count = 0
            
        except Exception as e:
            error_msg = str(e)
            print(f"⚠️ Error parsing email {e_id}: {error_msg}")
            
            if "OVERQUOTA" in error_msg or "exceeded" in error_msg.lower():
                failed_count += 1
                print(f"❌ Gmail Quota Exceeded! ({failed_count}/{MAX_FAILURES})")
                
                if failed_count >= MAX_FAILURES:
                    print(f"🛑 Stopping due to failures.")
                    break
                
                print(f"⏰ Waiting {OVERQUOTA_COOLDOWN}s...")
                time.sleep(OVERQUOTA_COOLDOWN)
                
                try:
                    mail.close()
                    mail.logout()
                    time.sleep(5)
                    mail = imaplib.IMAP4_SSL(IMAP_SERVER)
                    mail.login(EMAIL_USER, EMAIL_PASS)
                    mail.select("inbox")
                    print("✅ Reconnected")
                except:
                    print("❌ Reconnection failed, stopping.")
                    break
            else:
                failed_count += 1
                if failed_count >= MAX_FAILURES:
                    print(f"🛑 Stopping due to errors.")
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
        print("☕ No new papers to process.")
        return
    
    print(f"📑 Queue Ready: Analyzing {len(to_process)} papers today.")
    report_body, all_files, total_new, failed = "", [], 0, []
    
    for src in to_process:
        print(f"📝 Processing {total_new + len(failed) + 1}: {src.get('id', 'Document')}")
        content, ctype, path = fetch_content(src, save_dir=DOWNLOAD_DIR)
        if path:
            all_files.append(path)
        if content:
            print("🤖 Calling LLM for analysis...")
            ans = analyze_with_llm(content, ctype, src.get('url'))
            if "LLM Analysis Error" not in ans:
                report_body += f"## 📑 {src.get('id', 'Paper')}\n\n{ans}\n\n---\n\n"
                processed_ids.append(get_unique_id(src))
                total_new += 1
                continue
        failed.append(src)
    
    print(f"📊 Analysis complete. Success: {total_new}, Failed: {len(failed)}")
    
    final_report = f"# 📅 Literature Daily {datetime.date.today()}\n\n" + report_body
    if total_new > 0 or failed:
        print("📨 Packaging and sending email...")
        zip_file = "papers.zip" if all_files else None
        if zip_file:
            with zipfile.ZipFile(zip_file, 'w') as zf:
                for f in all_files:
                    zf.write(f, os.path.basename(f))
        
        if send_email_with_attachment(f"🤖 AI Daily (New:{total_new})", final_report, zip_file):
            print("📧 Email sent!")
        else:
            print("❌ Email failed.")
        
        if zip_file and os.path.exists(zip_file):
            os.remove(zip_file)
    
    save_history(processed_ids)
    print("💾 History saved.")

if __name__ == "__main__":
    main()
