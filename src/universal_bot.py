import os
import re
import requests
import pymupdf4llm
from openai import OpenAI
from habanero import Crossref
from bs4 import BeautifulSoup
import time

# --- 核心配置区 ---
API_KEY = os.environ.get("LLM_API_KEY")
BASE_URL = "https://api.siliconflow.cn/v1"

# 这里填入你指定的硅基流动模型ID
MODEL_NAME = "deepseek-ai/DeepSeek-R1-0528-Qwen3-8B"

client = OpenAI(api_key=API_KEY, base_url=BASE_URL)
cr = Crossref()

def detect_and_extract(text):
    """智能分拣：提取 ArXiv ID, DOI 或 PDF 链接"""
    result = {"type": None, "id": None, "url": None}
    
    # 1. ArXiv ID
    arxiv_match = re.search(r"arXiv ID:\s*(\d+\.\d+)", text)
    if arxiv_match:
        result["type"] = "arxiv"
        result["id"] = arxiv_match.group(1)
        result["url"] = f"https://arxiv.org/pdf/{result['id']}.pdf"
        return result

    # 2. DOI (Stork/文献鸟)
    doi_match = re.search(r"doi:\s*(10\.\d{4,9}/[-._;()/:A-Z0-9]+)", text, re.IGNORECASE)
    if doi_match:
        result["type"] = "doi"
        result["id"] = doi_match.group(1)
        # 尝试找 OA 链接，如果找不到后续逻辑会处理
        result["url"] = get_oa_link_from_doi(result["id"])
        return result

    # 3. 直接 PDF 链接 (Scholar)
    pdf_link_match = re.search(r'(https?://[^\s]+\.pdf)', text)
    if pdf_link_match:
        result["type"] = "direct_pdf"
        result["url"] = pdf_link_match.group(1)
        return result
    
    # 4. 普通网页链接 (Project MUSE)
    url_match = re.search(r'(https?://[^\s]+)', text)
    if url_match:
        result["type"] = "webpage"
        result["url"] = url_match.group(1)
        return result

    return None

def get_oa_link_from_doi(doi):
    """利用 Unpaywall API 查找 DOI 是否有免费 PDF"""
    try:
        email = "bot@example.com" # Unpaywall 要求
        r = requests.get(f"https://api.unpaywall.org/v2/{doi}?email={email}", timeout=10)
        data = r.json()
        if data.get('is_oa') and data.get('best_oa_location'):
            return data['best_oa_location']['url_for_pdf']
    except:
        pass
    return None

def fetch_content(source_data):
    """根据链接下载 PDF 或抓取摘要"""
    content = ""
    source_type = "Full Text"

    # A. 尝试下载 PDF
    if source_data["url"] and source_data["url"].endswith(".pdf"):
        print(f"📥 正在下载 PDF: {source_data['url']}")
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
            print(f"⚠️ PDF 下载失败: {e}")

    # B. 如果是 DOI 且没下载到 PDF -> 抓元数据
    if source_data["type"] == "doi":
        print("ℹ️ 无法获取 PDF，尝试抓取 Crossref 摘要...")
        try:
            work = cr.works(ids=source_data["id"])
            title = work['message'].get('title', [''])[0]
            abstract = work['message'].get('abstract', '无摘要信息')
            abstract = re.sub(r'<[^>]+>', '', abstract) # 清理 XML 标签
            content = f"# {title}\n\n## Abstract\n{abstract}"
            return content, "Abstract Only"
        except:
            pass

    # C. 普通网页抓取
    if source_data["type"] == "webpage":
        print("🌐 正在抓取网页文本...")
        try:
            headers = {"User-Agent": "Mozilla/5.0"}
            r = requests.get(source_data["url"], headers=headers, timeout=30)
            soup = BeautifulSoup(r.text, 'html.parser')
            # 移除导航栏等杂项
            for script in soup(["script", "style", "nav", "footer"]):
                script.decompose()
            content = soup.get_text()
            return content[:15000], "Webpage Content" # 截取前1.5万字
        except:
            pass
            
    return None, "Unknown"

def analyze_with_llm(content, content_type):
    """调用 LLM 进行分析"""
    prompt = f"""
    你是一个专业的学术研究助理。请分析以下文献内容（类型：{content_type}）。
    
    请输出一份结构清晰的 Markdown 报告：
    1. **标题与领域**: (推测文献所属的具体子领域)
    2. **一句话核心**: (TL;DR)
    3. **深度解析**:
       - **研究背景/痛点**: (解决了什么问题？)
       - **方法论/数据**: (如果是实证研究，请列出数据来源、模型；如果是理论，请列出核心论点)
       - **主要结论**: (具体的发现)
    4. **用户相关性**: 
       - 用户关注：社会科学、因果推断、中国政治、帝国史。
       - 请判断此文对用户的价值（高/中/低）并简述理由。

    内容如下：
    ---
    {content[:50000]} 
    ---
    """
    
    try:
        completion = client.chat.completions.create(
            model=MODEL_NAME, # <--- 这里已经修改为你指定的模型变量
            messages=[{"role": "user", "content": prompt}],
            temperature=0.3
        )
        return completion.choices[0].message.content
    except Exception as e:
        return f"LLM 分析出错: {e}"
