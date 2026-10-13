# -*- coding: utf-8 -*-
# B站视频下载音频转换分析内容助手 - Gradio版

import os
import sys
import time
import json
import base64
import hashlib
import hmac
import requests
import gradio as gr
from urllib import parse
from docx import Document

# 确保标准输出使用UTF-8
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')

# Configuration is loaded from environment variables or the local .env file.
from settings import (
    XFYUN_APPID, XFYUN_API_SECRET, LLM_API_KEY, LLM_API_URL, LLM_MODEL,
    DASHSCOPE_API_KEY, FFMPEG_PATH, COOKIES_FILE, CACHE_DIR, KB_DIR,
    ENABLE_DOCUMENT_ROUTING,
)
from retrieval import unique_queries, reciprocal_rank_fusion
import bilibili_kb

LFASR_HOST = "https://raasr.xfyun.cn/v2/api"
API_UPLOAD = "/upload"
API_GET_RESULT = "/getResult"


def generate_signa(appid, secret_key, ts):
    """生成讯飞API签名"""
    m2 = hashlib.md5()
    m2.update((appid + ts).encode("utf-8"))
    md5 = m2.hexdigest()
    md5_bytes = bytes(md5, encoding="utf-8")
    signa = hmac.new(secret_key.encode("utf-8"), md5_bytes, hashlib.sha1).digest()
    signa = base64.b64encode(signa).decode("utf-8")
    return signa


def query_llm(content, prompt="请对以下内容进行总结，提取核心观点和要点：", system_prompt=None, timeout=120, max_retries=2):
    """调用大模型（当前使用Kimi），支持超时与重试"""
    if not LLM_API_KEY:
        return "[未配置大模型API密钥，跳过总结步骤]\n\n请在 .env 中配置 LLM_API_KEY。"
    if not LLM_MODEL:
        return "API调用失败: 请在 .env 中配置账号可用的 LLM_MODEL。"

    headers = {
        "Content-Type": "application/json",
        "Authorization": "Bearer " + LLM_API_KEY
    }

    # 如果传入了system_prompt，说明content是完整的用户提示词
    if system_prompt:
        user_content = content
        system_content = system_prompt
    else:
        user_content = prompt + "\n\n" + content
        system_content = "你是一个专业的视频内容分析助手，擅长总结和提取要点。"

    data = {
        "model": LLM_MODEL,
        "messages": [
            {"role": "system", "content": system_content},
            {"role": "user", "content": user_content}
        ],
        "max_tokens": 4000
    }

    last_error = None
    for attempt in range(max_retries + 1):
        try:
            response = requests.post(LLM_API_URL, headers=headers, json=data, timeout=timeout)
            if response.status_code != 200:
                error_msg = f"API调用失败，状态码 {response.status_code}: {response.text}"
                # 非超时类 HTTP 错误不直接重试
                if response.status_code >= 500 and attempt < max_retries:
                    last_error = error_msg
                    time.sleep(2 ** attempt)
                    continue
                return error_msg
            result = response.json()
            if "choices" in result and len(result["choices"]) > 0:
                return result["choices"][0]["message"]["content"]
            else:
                return "API调用失败: " + str(result.get('error', {}).get('message', '未知错误'))
        except requests.exceptions.Timeout as e:
            last_error = f"调用大模型超时: {e}"
            if attempt < max_retries:
                print(f"[query_llm] 第 {attempt + 1} 次请求超时，{2 ** attempt}s 后重试...", flush=True)
                time.sleep(2 ** attempt)
                continue
            return last_error
        except Exception as e:
            last_error = f"调用大模型失败: {e}"
            if attempt < max_retries:
                print(f"[query_llm] 第 {attempt + 1} 次请求异常，{2 ** attempt}s 后重试...", flush=True)
                time.sleep(2 ** attempt)
                continue
            return last_error

    return last_error if last_error else "调用大模型失败: 未知错误"


# ==================== 核心功能 ====================

def prepare_cookies(cookies_path):
    """准备cookies文件，支持json和netscape txt格式"""
    # 如果上传的是json但实际内容是netscape格式，转换为txt
    try:
        with open(cookies_path, "r", encoding="utf-8") as f:
            content = f.read().strip()

        # 检测是否是Netscape格式
        if content.startswith("# Netscape HTTP Cookie File"):
            # 已经是Netscape格式，确保扩展名是.txt
            txt_path = COOKIES_FILE.replace(".json", ".txt")
            with open(txt_path, "w", encoding="utf-8") as f:
                f.write(content)
            return txt_path

        # 尝试解析为JSON格式
        try:
            cookies = json.loads(content)
            # JSON格式也转换为yt-dlp支持的格式
            txt_path = COOKIES_FILE.replace(".json", ".txt")
            with open(txt_path, "w", encoding="utf-8") as f:
                f.write("# Netscape HTTP Cookie File\n")
                f.write("# http://curl.haxx.se/rfc/cookie_spec.html\n")
                f.write("# This is a generated file!  Do not edit.\n\n")
                for cookie in cookies:
                    domain = cookie.get("domain", "")
                    flag = "TRUE" if domain.startswith(".") else "FALSE"
                    path = cookie.get("path", "/")
                    secure = "TRUE" if cookie.get("secure", False) else "FALSE"
                    expiry = str(cookie.get("expirationDate", 0)).split(".")[0]
                    name = cookie.get("name", "")
                    value = cookie.get("value", "")
                    if name and value:
                        f.write(f"{domain}\t{flag}\t{path}\t{secure}\t{expiry}\t{name}\t{value}\n")
            return txt_path
        except json.JSONDecodeError:
            # 不是JSON也不是Netscape，尝试按原样使用
            return cookies_path

    except Exception as e:
        return cookies_path


def extract_bv(video_url):
    """从B站URL中提取BV号"""
    import re
    match = re.search(r'BV[0-9A-Za-z]+', video_url)
    return match.group(0) if match else "unknown"


def download_video(video_url, cookies_path=None):
    """下载B站视频，自动尝试多个浏览器获取cookies，支持手动上传备用"""
    try:
        import yt_dlp

        # 按BV号创建缓存文件名，避免不同视频互相覆盖或被旧文件锁定
        bv = extract_bv(video_url)
        os.makedirs(CACHE_DIR, exist_ok=True)
        output_path = os.path.join(CACHE_DIR, bv)
        video_file = output_path + ".mp4"

        # 如果已存在同名缓存文件，直接复用
        if os.path.exists(video_file):
            return video_file, f"命中视频缓存：{video_file}"

        # 如果用户上传了cookies文件，优先使用上传的文件
        if cookies_path and os.path.exists(cookies_path):
            prepared_cookies = prepare_cookies(cookies_path)
            ydl_opts = {
                'cookiefile': prepared_cookies,
                'outtmpl': output_path,
                'format': 'bestvideo+bestaudio/best',
                'merge_output_format': 'mp4',
                'quiet': True,
                'no_warnings': True,
                'ffmpeg_location': FFMPEG_PATH,
            }
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                ydl.download([video_url])
            return video_file, "视频下载成功"

        # 自动尝试多个浏览器读取cookies
        # 注意：Windows上Chrome/Edge的cookie数据库可能被锁定或加密（DPAPI），Firefox通常最稳定
        browsers = ['firefox', 'edge', 'chrome']
        last_error = ""
        for browser in browsers:
            try:
                ydl_opts = {
                    'cookiesfrombrowser': (browser,),
                    'outtmpl': output_path,
                    'format': 'bestvideo+bestaudio/best',
                    'merge_output_format': 'mp4',
                    'quiet': True,
                    'no_warnings': True,
                    'ffmpeg_location': FFMPEG_PATH,
                }
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    ydl.download([video_url])
                return video_file, f"视频下载成功（使用{browser}浏览器cookies）"
            except Exception as e:
                last_error = str(e)
                # cookie相关错误继续尝试下一个浏览器
                if any(kw in last_error.lower() for kw in ["cookie", "database", "dpapi", "decrypt"]):
                    continue
                # 其他错误直接返回
                return None, "下载失败: " + last_error

        return None, "下载失败: 无法从浏览器读取cookies。建议：1) 在Firefox登录B站后重试；2) 关闭Chrome/Edge后重试；3) 手动上传cookies文件。\n详细错误: " + last_error
    except Exception as e:
        return None, "下载失败: " + str(e)


def get_video_info(video_url, cookies_path=None):
    """获取B站视频元数据（标题、UP主、时长等），优先使用已上传cookies"""
    try:
        import yt_dlp

        ydl_opts = {
            'quiet': True,
            'no_warnings': True,
        }

        # 如果用户上传了cookies文件，优先使用
        if cookies_path and os.path.exists(cookies_path):
            prepared_cookies = prepare_cookies(cookies_path)
            ydl_opts['cookiefile'] = prepared_cookies
        else:
            # 自动尝试浏览器cookies
            ydl_opts['cookiesfrombrowser'] = ('firefox',)

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(video_url, download=False)
            return {
                "title": info.get("title", "未知标题"),
                "uploader": info.get("uploader", "未知UP主"),
                "duration": info.get("duration", 0),
                "description": info.get("description", ""),
                "video_url": video_url
            }
    except Exception as e:
        return {
            "title": "未知标题",
            "uploader": "未知UP主",
            "duration": 0,
            "description": "",
            "video_url": video_url,
            "error": str(e)
        }


def extract_audio(video_path):
    """提取音频，输出文件与视频文件同名避免冲突"""
    if not os.path.exists(video_path):
        return None, "视频文件不存在"

    # 使用subprocess代替os.system，避免编码问题
    import subprocess
    # 输出音频文件名与视频文件名保持一致，避免被旧音频锁定
    base, _ = os.path.splitext(video_path)
    audio_path = base + "_16k.wav"

    # 如果同名音频已存在，直接复用
    if os.path.exists(audio_path):
        return audio_path, f"命中音频缓存：{audio_path}"

    cmd = [
        FFMPEG_PATH,
        "-i", video_path,
        "-ar", "16000",
        "-ac", "1",
        "-acodec", "pcm_s16le",
        audio_path,
        "-y"
    ]

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, encoding='utf-8', errors='ignore')
        if result.returncode == 0 and os.path.exists(audio_path):
            return audio_path, "音频提取成功"
        else:
            error_msg = result.stderr[-500:] if result.stderr else "未知错误"
            return None, "音频提取失败: " + error_msg
    except Exception as e:
        return None, "音频提取异常: " + str(e)


def upload_and_transcribe(audio_path):
    """上传音频并转写"""
    if not XFYUN_APPID or not XFYUN_API_SECRET:
        return None, "请在 .env 中配置 XFYUN_APPID 和 XFYUN_API_SECRET"
    if not os.path.exists(audio_path):
        return None, "音频文件不存在"

    try:
        file_len = os.path.getsize(audio_path)
        file_name = os.path.basename(audio_path)
        ts = str(int(time.time()))
        signa = generate_signa(XFYUN_APPID, XFYUN_API_SECRET, ts)

        param_dict = {
            "appId": XFYUN_APPID,
            "signa": signa,
            "ts": ts,
            "fileSize": file_len,
            "fileName": file_name,
            "duration": "200"
        }

        with open(audio_path, "rb") as f:
            file_data = f.read()

        upload_url = LFASR_HOST + API_UPLOAD + "?" + parse.urlencode(param_dict)
        response = requests.post(upload_url, headers={"Content-type": "application/json"}, data=file_data, timeout=600)
        result = response.json()

        if result.get("code") != "000000":
            return None, "上传失败: " + str(result.get('descInfo', result))

        order_id = result["content"]["orderId"]

        # 轮询结果
        max_wait = 300
        waited = 0
        while waited < max_wait:
            time.sleep(5)
            waited += 5

            ts_check = str(int(time.time()))
            signa_check = generate_signa(XFYUN_APPID, XFYUN_API_SECRET, ts_check)
            result_url = LFASR_HOST + API_GET_RESULT + "?appId=" + XFYUN_APPID + "&signa=" + signa_check + "&ts=" + ts_check + "&orderId=" + order_id + "&resultType=transfer"
            response = requests.get(result_url, headers={"Content-type": "application/json"}, timeout=30)
            result = response.json()

            if result.get("code") == "000000":
                status = result["content"].get("orderInfo", {}).get("status")
                if status == 4:
                    # 解析结果
                    order_result_str = result["content"].get("orderResult", "")
                    if order_result_str:
                        order_result = json.loads(order_result_str)
                        lattice = order_result.get("lattice", [])
                        text_result = []
                        for item in lattice:
                            json_str = item.get("json_1best", "{}")
                            json_obj = json.loads(json_str)
                            words = json_obj.get("st", {}).get("rt", [])
                            for word_list in words:
                                for w in word_list.get("ws", []):
                                    for cw in w.get("cw", []):
                                        text_result.append(cw.get("w", ""))
                            text_result.append("\n")
                        return "".join(text_result), "转写完成"
                    else:
                        return None, "转写结果为空"
                elif status == 5:
                    return None, "转写失败"

            if waited >= max_wait:
                return None, "转写超时"

        return None, "轮询异常"
    except Exception as e:
        return None, "转写异常: " + str(e)


def save_results(transcript_text, summary_text):
    """保存结果到文件"""
    # 保存转写结果
    with open("transcript_result.txt", "w", encoding="utf-8") as f:
        f.write(transcript_text)

    # 保存总结结果
    with open("summary_result.txt", "w", encoding="utf-8") as f:
        f.write(summary_text)

    # 保存到Word，若默认文件名被占用则使用时间戳命名
    doc = Document()
    doc.add_heading("B站视频分析报告", 0)
    doc.add_heading("一、完整转写", level=1)
    doc.add_paragraph(transcript_text)
    doc.add_heading("二、内容总结", level=1)
    doc.add_paragraph(summary_text)

    docx_name = "video_analysis_report.docx"
    try:
        doc.save(docx_name)
    except PermissionError:
        from datetime import datetime
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        docx_name = f"video_analysis_report_{timestamp}.docx"
        doc.save(docx_name)

    return f"文件保存成功：{docx_name}"


# ==================== 主流程函数 ====================

def process_video(video_url, cookies_path):
    """处理视频的完整流程，按BV号缓存，不同视频互不干扰"""
    yield "正在初始化..."

    bv = extract_bv(video_url)
    video_file = os.path.join(CACHE_DIR, bv + ".mp4")
    audio_file = os.path.join(CACHE_DIR, bv + "_16k.wav")

    # 步骤1: 下载视频（按BV号缓存，命中则直接复用）
    yield "步骤1/4: 正在下载视频..."
    video_path, msg = download_video(video_url, cookies_path)
    if not video_path:
        yield "ERROR: 下载失败: " + msg
        return
    yield msg

    # 步骤2: 提取音频（按BV号缓存，命中则直接复用）
    yield "步骤2/4: 正在提取音频..."
    audio_path, msg = extract_audio(video_path)
    if not audio_path:
        yield "ERROR: 音频提取失败: " + msg
        return
    yield msg

    # 获取视频元数据（标题、UP主等）
    yield "正在获取视频元数据..."
    video_metadata = get_video_info(video_url, cookies_path)
    video_title = video_metadata.get("title", video_url)

    # 步骤3: 上传转写
    yield "步骤3/4: 正在上传并转写（可能需要1-5分钟）..."
    transcript_text, msg = upload_and_transcribe(audio_path)
    if not transcript_text:
        yield "ERROR: 转写失败: " + msg
        return

    # 步骤4: 大模型总结
    yield "步骤4/4: 正在进行AI总结..."
    summary_text = query_llm(transcript_text)

    # 保存结果
    save_results(transcript_text, summary_text)

    # 沉淀到知识库
    kb_status = ""
    try:
        kb = bilibili_kb.KnowledgeBase(KB_DIR)
        # Keep service error messages out of the searchable summary corpus.
        summary_for_kb = summary_text
        if not LLM_API_KEY or not LLM_MODEL or any(
            marker in summary_text for marker in ("API调用失败", "调用大模型失败", "调用大模型超时")
        ):
            summary_for_kb = ""
        success = kb.ingest(video_url, video_title, transcript_text, summary_for_kb, metadata=video_metadata)
        stats = kb.get_stats()
        if success:
            kb_status = f"\n\n[知识库] 已沉淀《{video_title}》，当前共 {stats['documents']} 份资料，{stats['chunks']} 个文本块。"
        else:
            kb_status = "\n\n[知识库] 未入库，请检查 .env 中的 DASHSCOPE_API_KEY。"
    except Exception as e:
        kb_status = f"\n\n[知识库] 沉淀失败: {str(e)}"

    yield "SUCCESS: 处理完成！" + kb_status + "\n\n" + "【转写内容预览】" + "\n" + transcript_text[:300] + "..." + "\n\n" + "【AI总结】" + "\n" + summary_text


# ==================== 查询扩展函数 ====================

def expand_queries(question: str, num: int = 3) -> list:
    """使用 LLM 将用户问题扩展为多个子查询，用于跨文档检索"""
    if not LLM_API_KEY:
        return [question]

    prompt = f"""请把下面的用户问题拆解为 {num} 个更具体的检索子问题。
要求：
- 每个子问题关注原问题中的一个关键概念或方面
- 子问题之间尽量覆盖不同的语义维度，便于从知识库中检索到更多相关文档
- 保持简洁，每个子问题一行
- 不要输出编号、解释或其他内容，只输出子问题本身

用户问题：{question}

子问题："""

    try:
        result = query_llm(
            prompt,
            system_prompt="你是一个问题拆解助手，擅长把复杂问题分解为多个可用于向量检索的子问题。",
            timeout=60,
            max_retries=1
        )
        if not result or "失败" in result or "超时" in result:
            return [question]
        queries = [q.strip() for q in result.strip().split('\n') if q.strip()]
        # 去重并确保原问题在首位
        unique = []
        if question.strip() not in queries:
            unique.append(question.strip())
        for q in queries:
            if q not in unique:
                unique.append(q)
        return unique[:num + 1]
    except Exception:
        return [question]


def route_documents(question: str, metadata_chunks: list) -> list:
    """使用 LLM 根据问题判断哪些文档可能相关，返回相关文档索引列表"""
    if not LLM_API_KEY or not metadata_chunks:
        return []

    doc_list = []
    for i, chunk in enumerate(metadata_chunks):
        title = chunk.get("title", "") or f"文档{i + 1}"
        doc_list.append(f"{i + 1}. {title}")
    doc_list_text = "\n".join(doc_list)

    prompt = f"""请根据用户问题，判断下面哪些文档可能与之相关。
要求：
- 只返回相关文档的编号，用逗号分隔
- 如果没有相关文档，返回"无"
- 不要输出任何解释、编号标题或其他内容

用户问题：{question}

文档列表：
{doc_list_text}

相关文档编号："""

    try:
        result = query_llm(
            prompt,
            system_prompt="你是一个文档路由助手，只根据用户问题和文档标题判断相关性，输出编号。",
            timeout=60,
            max_retries=1
        )
        if not result or "失败" in result or "超时" in result or "无" in result:
            return []

        result = result.strip()
        related = []
        for part in result.replace("，", ",").split(","):
            part = part.strip()
            # 支持 "1." 或 "1" 这样的格式
            part = part.rstrip(".").strip()
            if part.isdigit():
                idx = int(part) - 1
                if 0 <= idx < len(metadata_chunks):
                    related.append(idx)
        return related
    except Exception:
        return []


# ==================== 知识库问答函数 ====================

def answer_from_kb(question, top_k=5):
    """基于知识库回答问题（支持多查询扩展与跨文档整合）"""
    if not question or not question.strip():
        return "请输入问题", ""

    if not bilibili_kb.DASHSCOPE_API_KEY:
        return "未配置DASHSCOPE_API_KEY，无法检索知识库。请在 .env 中配置。", ""

    try:
        kb = bilibili_kb.KnowledgeBase(KB_DIR)
        if not kb.chunks:
            return "知识库为空，请先分析视频或上传文档。", ""

        # 使用批量检索，减少 embedding 调用次数
        main_question = question.strip()
        expand_questions = expand_queries(main_question, num=3)

        # Fuse all query rankings before applying the context budget.
        queries = unique_queries(main_question, expand_questions)
        result_lists = kb.query_batch(queries, top_k=top_k)
        retrieved = reciprocal_rank_fusion(result_lists, top_k=top_k)
        seen_indices = {chunk.get("index", id(chunk)) for chunk in retrieved}

        # 组合结果：标题兜底 + 补充内容 优先放在前面，然后是多次检索结果
        if ENABLE_DOCUMENT_ROUTING:
            # 智能文档路由兜底：使用 LLM 判断问题可能涉及哪些文档，强制召回相关文档
            metadata_chunks = [c for c in kb.chunks if c.get("source") == "metadata"]
            title_matched = []
            try:
                related_indices = route_documents(question.strip(), metadata_chunks)
                for idx in related_indices:
                    chunk = metadata_chunks[idx]
                    chunk_idx = chunk.get("index")
                    if chunk_idx is None:
                        chunk_idx = id(chunk)
                    if chunk_idx not in seen_indices:
                        seen_indices.add(chunk_idx)
                        title_matched.append(chunk)
            except Exception:
                pass

            # 如果某个文档的 metadata 被命中，自动补充该文档的内容片段
            # 避免标题匹配但正文未被召回的情况
            supplementary = []
            seen_doc_keys = set()
            for chunk in retrieved + title_matched:
                if chunk.get("source") == "metadata":
                    doc_key = chunk.get("video_url")
                    if doc_key and doc_key not in seen_doc_keys:
                        seen_doc_keys.add(doc_key)
                        added_for_doc = 0
                        for c in kb.chunks:
                            if (c.get("video_url") == doc_key
                                    and c.get("source") in ("transcript", "summary")):
                                idx = c.get("index")
                                if idx is None:
                                    idx = id(c)
                                if idx not in seen_indices:
                                    seen_indices.add(idx)
                                    supplementary.append(c)
                                    added_for_doc += 1
                                    if added_for_doc >= 3:
                                        break

            retrieved = title_matched + supplementary + retrieved

        # 限制总片段数为 top_k，确保用户设置的"单次检索片段数"就是最终参考来源的片段数
        retrieved = retrieved[:top_k]

        if not retrieved:
            return "知识库为空，请先分析视频并沉淀内容。", ""

        # 构建上下文，限制单个片段长度，避免 prompt 过长导致模型输出被截断
        max_chunk_len = 500
        context_parts = []
        sources = []
        for i, chunk in enumerate(retrieved, 1):
            text = chunk['text']
            if len(text) > max_chunk_len:
                text = text[:max_chunk_len] + "..."
            title = chunk.get('title', '未知来源')
            context_parts.append(f"[片段{i} | 来源：{title}]\n{text}")
            sources.append(f"片段{i}：{title}（{chunk.get('source', '未知来源')}）")
        context = "\n\n".join(context_parts)

        # 调用Kimi生成答案，强调跨文档整合
        prompt = f"""你是一个专业的视频内容分析助手。请根据以下从多个视频和文档中检索到的内容，详细回答用户的问题。
每个片段开头都已标注来源标题，请严格按照来源归属信息组织回答，不要把 A 来源的内容归到 B 来源下。
检索内容可能来自不同来源，请进行综合整合：
- 如果不同来源的观点一致，请合并总结并适当展开
- 如果不同来源的观点有差异，请分别说明并给出判断
- 如果检索内容中没有直接相关信息，请明确说明
- 回答时请分点论述，给出具体例子和操作细节，避免过于简略

检索内容：
{context}

用户问题：{question}

请给出详细、准确且经过整合的回答，结构上请包含：
1. 关键概念解释
2. 各来源的核心方法
3. 如何把不同来源整合应用到用户问题上
4. 注意事项或风险提示"""

        answer = query_llm(
            prompt,
            system_prompt="你是一个专业的视频内容分析助手，擅长整合多个来源的信息并回答用户问题。",
            timeout=120,
            max_retries=2
        )

        # 如果最终答案生成失败，给出友好兜底，避免把超时错误原文返回给用户
        if not answer or (isinstance(answer, str) and ("调用大模型失败" in answer or "API调用失败" in answer or "超时" in answer)):
            sources_text = "\n".join(sources)
            fallback = (
                "很抱歉，当前大模型调用超时或失败，无法基于检索内容生成完整答案。\n\n"
                "您已检索到以下参考来源，可供手动查看：\n" + sources_text
            )
            return fallback, f"参考来源：\n{sources_text}"

        sources_text = "\n".join(sources)
        return answer, f"参考来源：\n{sources_text}"
    except Exception as e:
        return f"知识库问答失败: {str(e)}", ""


# ==================== 文档沉淀函数 ====================

def ingest_document(file_obj, doc_title, doc_source):
    """上传本地文档并沉淀到知识库"""
    if not file_obj:
        return "请先上传文档"

    # Gradio File 组件返回的是文件路径字符串
    file_path = str(file_obj)
    file_name = os.path.basename(file_path)

    if not doc_title or not doc_title.strip():
        # 去掉扩展名作为标题
        doc_title = os.path.splitext(file_name)[0]
    else:
        doc_title = doc_title.strip()

    # 读取文档内容
    try:
        if file_name.lower().endswith(".docx"):
            doc = Document(file_path)
            text = "\n\n".join([p.text.strip() for p in doc.paragraphs if p.text.strip()])
        elif file_name.lower().endswith(".txt"):
            # 优先尝试 UTF-8，失败则尝试 GBK
            try:
                with open(file_path, "r", encoding="utf-8") as f:
                    text = f.read()
            except UnicodeDecodeError:
                with open(file_path, "r", encoding="gbk") as f:
                    text = f.read()
        else:
            return "仅支持 .docx 和 .txt 格式的文档"
    except Exception as e:
        return f"读取文档失败: {str(e)}"

    if not text or not text.strip():
        return "文档内容为空"

    # 来源标识，未填写则使用 docx://文件名
    source_id = doc_source.strip() if doc_source and doc_source.strip() else f"docx://{file_name}"

    try:
        kb = bilibili_kb.KnowledgeBase(KB_DIR)
        success = kb.ingest(
            video_url=source_id,
            title=doc_title,
            transcript=text,
            summary="",
            metadata={"source": "document", "file_name": file_name, "doc_source": source_id}
        )
        if success:
            stats = kb.get_stats()
            return f"沉淀成功！《{doc_title}》已加入知识库。当前共 {stats['documents']} 个文档，{stats['chunks']} 个文本块。"
        else:
            return "沉淀失败，请检查 DASHSCOPE_API_KEY 是否配置"
    except Exception as e:
        return f"沉淀失败: {str(e)}"


# ==================== Gradio界面 ====================

with gr.Blocks(title="B站视频内容分析助手") as demo:
    gr.Markdown("# B站视频内容分析助手")
    gr.Markdown("输入 B 站视频链接，提取音频内容、生成总结并沉淀到知识库。依次尝试 Firefox、Edge、Chrome 登录态，也可手动上传 Cookies 文件。")

    with gr.Tab("视频分析"):
        with gr.Row():
            with gr.Column():
                video_url = gr.Textbox(label="B站视频链接", placeholder="请输入B站视频URL，如：https://www.bilibili.com/video/BV1CLEc6dEzF")
                cookies_file = gr.File(label="B站Cookies文件（可选）", file_types=[".json", ".txt"])
                submit_btn = gr.Button("开始分析", variant="primary")

            with gr.Column():
                progress = gr.Textbox(label="进度", lines=10, interactive=False)

        with gr.Row():
            with gr.Column():
                transcript_output = gr.Textbox(label="转写内容", lines=15, interactive=False)
            with gr.Column():
                summary_output = gr.Textbox(label="AI总结", lines=15, interactive=False)

    with gr.Tab("文档沉淀"):
        gr.Markdown("### 上传本地文档沉淀到知识库")

        with gr.Row():
            with gr.Column():
                doc_file = gr.File(label="上传文档", file_types=[".docx", ".txt"])
                doc_title_input = gr.Textbox(label="文档标题（可选）", placeholder="留空将使用文件名")
                doc_source_input = gr.Textbox(label="来源标识（可选）", placeholder="留空将使用 docx://文件名")
                ingest_btn = gr.Button("沉淀到知识库", variant="primary")

            with gr.Column():
                ingest_result = gr.Textbox(label="沉淀结果", lines=8, interactive=False)

    with gr.Tab("知识库问答"):
        gr.Markdown("### 基于已沉淀的视频内容进行问答")

        with gr.Row():
            with gr.Column():
                kb_question = gr.Textbox(label="问题", placeholder="请输入关于已分析视频的问题", lines=3)
                top_k_slider = gr.Slider(label="参考来源最大片段数", minimum=1, maximum=20, value=10, step=1)
                kb_query_btn = gr.Button("提问", variant="primary")

            with gr.Column():
                kb_answer = gr.Textbox(label="回答", lines=35, max_lines=80, interactive=False, autoscroll=False)
                kb_sources = gr.Textbox(label="参考来源", lines=8, interactive=False)

    def main_process(video_url, cookies_file):
        if not video_url:
            return "请输入视频链接", "", ""

        cookies_path = None
        # 如果用户上传了cookies文件则使用，否则自动从Chrome读取
        if cookies_file:
            # Gradio上传的文件是路径字符串
            if hasattr(cookies_file, 'read'):
                # 文件对象
                with open(COOKIES_FILE, "wb") as f:
                    f.write(cookies_file.read())
                cookies_path = COOKIES_FILE
            else:
                # 文件路径字符串
                cookies_path = str(cookies_file)
                # 复制到标准位置
                import shutil
                shutil.copy(cookies_path, COOKIES_FILE)

        result_text = ""
        for msg in process_video(video_url, cookies_path):
            result_text = msg
            if "ERROR:" in msg:
                yield msg.replace("ERROR:", ""), "", ""
                return
            elif "SUCCESS:" in msg:
                content = msg.replace("SUCCESS: ", "")
                parts = content.split("【AI总结】\n")
                if len(parts) >= 2:
                    transcript_part = parts[0].replace("处理完成！\n\n【转写内容预览】\n", "")
                    summary_part = parts[1]
                    yield "处理完成！", transcript_part, summary_part
                else:
                    yield "处理完成！", content, ""
            else:
                yield msg, "", ""

    submit_btn.click(
        fn=main_process,
        inputs=[video_url, cookies_file],
        outputs=[progress, transcript_output, summary_output]
    )

    ingest_btn.click(
        fn=ingest_document,
        inputs=[doc_file, doc_title_input, doc_source_input],
        outputs=[ingest_result]
    )

    kb_query_btn.click(
        fn=answer_from_kb,
        inputs=[kb_question, top_k_slider],
        outputs=[kb_answer, kb_sources]
    )


if __name__ == "__main__":
    # 支持通过环境变量 GRADIO_SERVER_PORT 覆盖默认端口
    server_port = int(os.environ.get("GRADIO_SERVER_PORT", "7860"))
    demo.launch(server_name=os.environ.get("GRADIO_SERVER_NAME", "127.0.0.1"), server_port=server_port)
