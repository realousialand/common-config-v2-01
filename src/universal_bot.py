import os
import re
import requests
import pymupdf4llm
from openai import OpenAI
from habanero import Crossref
from bs4 import BeautifulSoup
import time
import hashlib

# --- 核心配置区 ---
API_KEY = os.environ.get("LLM_API_KEY")
BASE_URL = "https://api.siliconflow.cn/v1"

# 🟢 修改点：优先读取环境变量，读不到才用默认值
# 这样代码里就不显示具体的模型名了
MODEL_NAME = os.environ.get("LLM_MODEL_NAME", "deepseek-ai/DeepSeek-R1-0528-Qwen3-8B")

client = OpenAI(api_key=API_KEY, base_url=BASE_URL)
cr = Crossref()

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

def detect_and_extract_all(text):
    """提取所有文献链接"""
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

    # 3. PDF Links
    for match in re.finditer(r'(https?://[^\s]+\.pdf)', text, re.IGNORECASE):
        url = match.group(1)
        if any(x in url for x in seen_ids): continue
        url_hash = hashlib.md5(url.encode()).hexdigest()
        if url_hash not in seen_ids:
            results.append({"type": "direct_pdf", "id": None, "url": url})
            seen_ids.add(url_hash)

    return results

def fetch_content(source_data, save_dir=None):
    """下载并提取内容"""
    content = ""
    saved_file_path = None

    # A. PDF 下载
    if source_data["url"] and source_data["url"].endswith(".pdf"):
        print(f"    📥 [下载] {source_data['url']}")
        time.sleep(3) 
        try:
            headers = {"User-Agent": "Mozilla/5.0"}
            r = requests.get(source_data["url"], headers=headers, timeout=60)
            if r.status_code == 200:
                file_id = source_data.get('id') or hashlib.md5(source_data['url'].encode()).hexdigest()
                safe_name = re.sub(r'[\\/*?:"<>|]', '_', file_id)
                filename = f"temp_{safe_name}.pdf"
                if save_dir:
                    filename = os.path.join(save_dir, f"{safe_name}.pdf")

                with open(filename, "wb") as f:
                    f.write(r.content)
                
                content = pymupdf4llm.to_markdown(filename)
                
                if save_dir:
                    saved_file_path = filename
                else:
                    os.remove(filename)
                    
                return content, "PDF Full Text", saved_file_path
        except Exception as e:
            print(f"    ⚠️ PDF 下载失败: {e}")

    # B. DOI 摘要
    if source_data["type"] == "doi":
        print(f"    ℹ️ [元数据] 抓取摘要 DOI: {source_data['id']}")
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
    """LLM 分析函数"""
    prompt = f"""
    请作为我的学术助手，基于以下提供的文献内容执行任务。
    【文献内容来源】：{content_type}
    【已知链接】：{source_url}

    ### 🎨 视觉增强指令 (重要)：
    在分析过程中，如果遇到**复杂的模型架构、算法流程、生物机制、关键数据图表**或**抽象概念**，为了帮助读者理解，请在相关段落后插入图片搜索标签。
    - **格式**：`
` 
    - **要求**：X 必须是具体、准确的搜索关键词（英文为佳）。
    - **示例**：
      - 讲到模型结构时插入：``
      - 讲到实验结果时插入：``
    - **原则**：只在有教育/解释意义时插入，不要为了美观而插入。

    ### 📝 任务步骤（请输出 Markdown 格式）：
    1. **确认并复述文献基本信息**：从文中提取并补全：标题、作者、期刊/会议、年份、关键词。
    2. **研究领域与影响力推断**。
    3. **研究现状与缺口**。
    4. **关键技术与创新**：(在此处若涉及架构，请务必插入  标签)
    5. **核心结论**。
    6. **术语解释**：解释2-3个专业术语 (配合图片标签辅助解释)。
    7. **优势与贡献**。
    8. **局限性与未来方向**。
    9. **相关文献推荐**：推荐3-5篇。
    10. **学术搜索模拟**：给出3个通过 Google Scholar 或 ArXiv 进一步研究的建议关键词组合，格式为：`- 关键词: [解释]`。
    11. **DOI与链接**：提供DOI或替代链接。
    12. **量化分析提取**（如适用）：Data/Dataset、变量、模型、统计方法、结果。

    ---
    {content[:50000]} 
    ---
    """
    try:
        # 🟢 这里的 MODEL_NAME 读取自环境变量，建议使用 R1 或 V3
        completion = client.chat.completions.create(
            model=MODEL_NAME,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.3
        )
        analysis = completion.choices[0].message.content
        
        # 🟢【清洗逻辑】去除 LLM 习惯性添加的 Markdown 代码块标记
        analysis = analysis.replace("```markdown", "").replace("```", "").strip()
        
        return analysis
        
    except Exception as e:
        return f"LLM 分析出错: {e}"
