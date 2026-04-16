import os
import json
import httpx
from fastapi import FastAPI, Request, BackgroundTasks
from supabase import create_client, Client
from pydantic import BaseModel
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI()

# 允许你的前端域名访问
origins = [
    "https://lde-console.vercel.app", # 你的前端地址
    "http://localhost:3000",          # 本地调试
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,           # 允许跨域
    allow_credentials=True,
    allow_methods=["*"],             # 允许所有方法 (GET, POST等)
    allow_headers=["*"],             # 允许所有请求头
)

# 环境变量配置
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY")
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY")
DEEPSEEK_BASE_URL = "https://api.deepseek.com/v1"

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

class QCExecutor:
    def __init__(self):
        self.client = httpx.AsyncClient(base_url=DEEPSEEK_BASE_URL, timeout=60.0)

    async def run_lde_logic_check(self, content: str, context_bridge: str, metadata: dict):
        """
        使用 DeepSeek 进行针对马来西亚法律 (Akta) 的高精度校验
        """
        prompt = f"""
        你现在是 Logos Data Engine (LDE) 的核心质检模块 Aequitas。
        你的任务是验证法律分块（Legal Chunk）的确定性与逻辑完整性。

        [待校验内容]
        {content}

        [上下文桥接信息]
        {context_bridge}

        [元数据]
        {json.dumps(metadata)}

        请严格检查以下维度：
        1. 逻辑断裂 (Logic Break)：检查分块是否在法律条文（如 Section, Subsection）中间被生硬切断。
        2. 定义丢失 (Definition Loss)：分块中提到的核心术语是否在 metadata 或 bridge 中有定义支持。
        3. 事实准确性：比对 content 与 context_bridge，确认提取过程中是否存在幻觉。

        必须以 JSON 格式返回，包含以下字段：
        - logic_score: 0.0-1.0
        - continuity_score: 0.0-1.0
        - hallucination_detected: boolean
        - feedback: 简短的专业评估说明
        - suggested_action: "pass", "re-chunk", 或 "flag_for_human"
        """

        response = await self.client.post(
            "/chat/completions",
            headers={"Authorization": f"Bearer {DEEPSEEK_API_KEY}"},
            json={
                "model": "deepseek-chat",
                "messages": [{"role": "user", "content": prompt}],
                "response_format": {"type": "json_object"}
            }
        )
        return response.json()['choices'][0]['message']['content']

qc_executor = QCExecutor()

async def process_quality_check(payload: dict):
    record = payload.get('record')
    if not record:
        return

    chunk_id = record['id']
    workspace_id = record['workspace_id']
    
    # 1. 执行 DeepSeek 质检
    raw_qc_output = await qc_executor.run_lde_logic_check(
        content=record['raw_markdown'],
        context_bridge=record.get('context_bridge', ''),
        metadata=record.get('extracted_metadata', {})
    )
    
    qc_data = json.loads(raw_qc_output)
    
    # 2. 写入影子质检表 qc_reports
    supabase.table("qc_reports").insert({
        "staging_id": chunk_id,
        "workspace_id": workspace_id,
        "logic_integrity_score": qc_data['logic_score'],
        "context_continuity_score": qc_data['continuity_score'],
        "is_hallucinated": qc_data['hallucination_detected'],
        "detailed_feedback": qc_data,
        "suggested_fix": qc_data['feedback']
    }).execute()

    # 3. 驱动 LDE 状态流转
    # 只有评分 > 0.85 且无幻觉的数据才标记为 processed
    is_passed = qc_data['logic_score'] > 0.85 and not qc_data['hallucination_detected']
    
    final_status = 'processed' if is_passed else 'failed'
    
    supabase.table("document_chunks_staging").update({
        "status": final_status,
        "error_log": qc_data['feedback'] if not is_passed else None,
        "updated_at": "now()"
    }).eq("id", chunk_id).execute()

@app.post("/webhook/qc-trigger")
async def handle_supabase_webhook(request: Request, background_tasks: BackgroundTasks):
    # 验证 Webhook Payload
    payload = await request.json()
    
    # 立即进入异步处理，防止 Supabase 5秒超时限制
    background_tasks.add_task(process_quality_check, payload)
    
    return {"status": "accepted", "message": "LDE Quality Check initiated."}