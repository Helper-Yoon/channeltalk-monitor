# main.py - 아정당 미답변 상담 모니터링 (심플 버전)

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Dict, List, Optional, Set
import httpx
import os

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, HTMLResponse
import redis.asyncio as redis
from pydantic import BaseModel
import uvicorn

# 로깅 설정
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# 채널톡 API 설정
CHANNEL_ID = "197228"
API_ACCESS_KEY = "688a26176fcb19aebf8b"
API_SECRET = "a0db6c38b95c8ec4d9bb46e7c653b3e2"
CHANNEL_API_BASE = "https://api.channel.io/open/v5"

# Redis 설정
REDIS_URL = os.getenv("REDIS_URL", "redis://red-d2ct46buibrs738rintg:6379")
WEBHOOK_TOKEN = "80ab2d11835f44b89010c8efa5eec4b4"

# 팀 매핑
TEAM_MAPPING = {
    # 매니저 ID나 이름을 기준으로 팀 매핑 (실제 데이터로 수정 필요)
    "default": "SNS 1팀"
}

app = FastAPI(title="아정당 미답변 상담")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

class Consultation(BaseModel):
    id: str
    team: str  # 담당자 팀
    manager: str  # 담당자
    last_message: str  # 고객 마지막 메시지
    wait_minutes: int  # 대기 시간(분)
    last_message_time: datetime

class ChannelAPI:
    def __init__(self):
        self.headers = {
            "x-access-key": API_ACCESS_KEY,
            "x-access-secret": API_SECRET,
            "Content-Type": "application/json"
        }
        self.client = httpx.AsyncClient(timeout=30.0)
        
    async def get_managers(self) -> Dict[str, str]:
        """매니저 목록 조회 및 팀 매핑"""
        try:
            response = await self.client.get(
                f"{CHANNEL_API_BASE}/managers",
                headers=self.headers
            )
            
            if response.status_code == 200:
                managers = response.json().get("managers", [])
                manager_teams = {}
                
                for manager in managers:
                    manager_id = manager.get("id")
                    manager_name = manager.get("name", "")
                    
                    # 이름이나 설명에서 팀 정보 추출
                    team = "미배정"
                    if "SNS 1" in manager_name or "SNS1" in manager_name:
                        team = "SNS 1팀"
                    elif "SNS 2" in manager_name or "SNS2" in manager_name:
                        team = "SNS 2팀"
                    elif "SNS 3" in manager_name or "SNS3" in manager_name:
                        team = "SNS 3팀"
                    elif "SNS 4" in manager_name or "SNS4" in manager_name:
                        team = "SNS 4팀"
                    elif "의정부" in manager_name:
                        team = "의정부 SNS팀"
                    
                    manager_teams[manager_id] = {
                        "name": manager_name,
                        "team": team
                    }
                
                return manager_teams
            return {}
            
        except Exception as e:
            logger.error(f"매니저 조회 실패: {e}")
            return {}
    
    async def get_unanswered(self) -> List[Dict]:
        """미답변 상담 조회"""
        try:
            # 열린 상담 조회
            response = await self.client.get(
                f"{CHANNEL_API_BASE}/user-chats",
                headers=self.headers,
                params={
                    "state": "opened",
                    "limit": 500,
                    "sortOrder": "-updatedAt"
                }
            )
            
            if response.status_code != 200:
                return []
            
            chats = response.json().get("userChats", [])
            unanswered = []
            
            # 매니저 정보 가져오기
            managers = await self.get_managers()
            
            for chat in chats:
                chat_id = chat.get("id")
                
                # 마지막 메시지 확인
                msg_response = await self.client.get(
                    f"{CHANNEL_API_BASE}/user-chats/{chat_id}/messages",
                    headers=self.headers,
                    params={"limit": 1, "sortOrder": "-createdAt"}
                )
                
                if msg_response.status_code == 200:
                    messages = msg_response.json().get("messages", [])
                    if messages and messages[0].get("personType") == "user":
                        # 담당자 정보
                        assignee = chat.get("assignee", {})
                        manager_id = assignee.get("id", "")
                        manager_info = managers.get(manager_id, {})
                        
                        unanswered.append({
                            "id": chat_id,
                            "team": manager_info.get("team", "미배정"),
                            "manager": manager_info.get("name", assignee.get("name", "미배정")),
                            "last_message": messages[0].get("message", ""),
                            "last_message_time": messages[0].get("createdAt", "")
                        })
            
            return unanswered
            
        except Exception as e:
            logger.error(f"미답변 조회 실패: {e}")
            return []

class Manager:
    def __init__(self, redis_client: redis.Redis, api: ChannelAPI):
        self.redis = redis_client
        self.api = api
        
    async def sync(self):
        """API 동기화"""
        try:
            unanswered = await self.api.get_unanswered()
            
            # Redis 초기화
            await self.redis.delete("consultations")
            
            # 데이터 저장
            pipe = self.redis.pipeline()
            for chat in unanswered:
                # 대기시간 계산
                msg_time = datetime.fromisoformat(chat["last_message_time"].replace("Z", "+00:00"))
                wait_minutes = int((datetime.now(timezone.utc) - msg_time).total_seconds() / 60)
                
                consultation = Consultation(
                    id=chat["id"],
                    team=chat["team"],
                    manager=chat["manager"],
                    last_message=chat["last_message"],
                    wait_minutes=wait_minutes,
                    last_message_time=msg_time
                )
                
                pipe.hset("consultations", chat["id"], consultation.json())
            
            await pipe.execute()
            
            logger.info(f"✅ 동기화 완료: {len(unanswered)}개")
            
            # WebSocket 브로드캐스트
            await self.broadcast_update()
            
        except Exception as e:
            logger.error(f"동기화 실패: {e}")
    
    async def process_webhook(self, data: Dict):
        """웹훅 처리"""
        try:
            event_type = data.get("type")
            
            if event_type == "message":
                message = data.get("message", {})
                chat_id = message.get("chatId")
                person_type = message.get("personType")
                
                if person_type == "user":
                    # 고객 메시지 - 미답변 추가
                    await self.add_unanswered(chat_id)
                elif person_type == "manager":
                    # 상담사 응답 - 제거
                    await self.remove_consultation(chat_id)
                    
        except Exception as e:
            logger.error(f"웹훅 처리 실패: {e}")
    
    async def add_unanswered(self, chat_id: str):
        """미답변 추가"""
        # API에서 상세 정보 가져와서 추가
        await self.sync()  # 간단하게 전체 동기화
    
    async def remove_consultation(self, chat_id: str):
        """상담 제거"""
        await self.redis.hdel("consultations", chat_id)
        await self.broadcast_update()
    
    async def get_all(self) -> List[Dict]:
        """전체 조회"""
        try:
            data = await self.redis.hgetall("consultations")
            consultations = []
            
            for item in data.values():
                consultation = json.loads(item)
                # 대기시간 재계산
                msg_time = datetime.fromisoformat(consultation["last_message_time"])
                wait_minutes = int((datetime.now(timezone.utc) - msg_time).total_seconds() / 60)
                consultation["wait_minutes"] = wait_minutes
                consultations.append(consultation)
            
            # 대기시간 순 정렬
            consultations.sort(key=lambda x: x["wait_minutes"], reverse=True)
            
            return consultations
            
        except Exception as e:
            logger.error(f"조회 실패: {e}")
            return []
    
    async def broadcast_update(self):
        """WebSocket 브로드캐스트"""
        consultations = await self.get_all()
        message = json.dumps({
            "type": "update",
            "data": consultations
        })
        
        disconnected = set()
        for ws in active_websockets:
            try:
                await ws.send_text(message)
            except:
                disconnected.add(ws)
        
        active_websockets.difference_update(disconnected)
    
    async def periodic_sync(self):
        """주기적 동기화"""
        while True:
            await asyncio.sleep(10)
            await self.sync()

# 전역 변수
redis_client: Optional[redis.Redis] = None
api_client: Optional[ChannelAPI] = None
manager: Optional[Manager] = None
active_websockets: Set[WebSocket] = set()

@app.on_event("startup")
async def startup():
    global redis_client, api_client, manager
    
    try:
        redis_client = redis.from_url(REDIS_URL, decode_responses=True)
        await redis_client.ping()
        logger.info("✅ Redis 연결")
        
        api_client = ChannelAPI()
        manager = Manager(redis_client, api_client)
        
        # 초기 동기화
        await manager.sync()
        
        # 주기적 동기화
        asyncio.create_task(manager.periodic_sync())
        
        logger.info("✅ 서버 시작")
        
    except Exception as e:
        logger.error(f"❌ 시작 실패: {e}")

@app.on_event("shutdown")
async def shutdown():
    if redis_client:
        await redis_client.close()

@app.post("/webhook")
async def webhook(request: Request, token: str = Query(...)):
    if token != WEBHOOK_TOKEN:
        raise HTTPException(status_code=401)
    
    try:
        body = await request.json()
        await manager.process_webhook(body)
        return {"status": "ok"}
    except Exception as e:
        logger.error(f"웹훅 실패: {e}")
        return {"status": "error"}

@app.get("/api/consultations")
async def get_consultations():
    consultations = await manager.get_all()
    return JSONResponse(consultations)

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    active_websockets.add(websocket)
    
    try:
        # 초기 데이터
        consultations = await manager.get_all()
        await websocket.send_json({
            "type": "initial",
            "data": consultations
        })
        
        while True:
            await websocket.receive_text()
            
    except WebSocketDisconnect:
        active_websockets.remove(websocket)

@app.get("/health")
async def health():
    try:
        await redis_client.ping()
        return {"status": "ok"}
    except:
        raise HTTPException(status_code=503)

@app.get("/")
async def index():
    """메인 페이지"""
    html_content = """<!DOCTYPE html>
<html lang="ko">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>미답변 상담</title>
    <style>
        * {
            margin: 0;
            padding: 0;
            box-sizing: border-box;
        }

        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
            background: #0A0B0E;
            color: #FFFFFF;
            padding: 20px;
        }

        .header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 20px;
            padding-bottom: 10px;
            border-bottom: 1px solid #2A2E38;
        }

        .title {
            font-size: 24px;
            font-weight: 600;
            color: #0066CC;
        }

        .status {
            display: flex;
            align-items: center;
            gap: 8px;
            font-size: 14px;
            color: #94A3B8;
        }

        .status-dot {
            width: 8px;
            height: 8px;
            border-radius: 50%;
            background: #22C55E;
        }

        .status-dot.disconnected {
            background: #EF4444;
        }

        .table-container {
            background: #12141A;
            border-radius: 8px;
            overflow: hidden;
            border: 1px solid #2A2E38;
        }

        table {
            width: 100%;
            border-collapse: collapse;
        }

        thead {
            background: #1A1D24;
        }

        th {
            text-align: left;
            padding: 12px 16px;
            font-size: 12px;
            font-weight: 600;
            color: #94A3B8;
            text-transform: uppercase;
            letter-spacing: 0.5px;
            border-bottom: 1px solid #2A2E38;
        }

        tbody tr {
            border-bottom: 1px solid #2A2E38;
            cursor: pointer;
            transition: background 0.1s;
        }

        tbody tr:hover {
            background: #1A1D24;
        }

        td {
            padding: 12px 16px;
            font-size: 14px;
            color: #E8EAED;
        }

        .team {
            font-weight: 500;
            color: #0066CC;
        }

        .manager {
            color: #94A3B8;
        }

        .message {
            max-width: 500px;
            overflow: hidden;
            text-overflow: ellipsis;
            white-space: nowrap;
            color: #E8EAED;
        }

        .wait-time {
            font-weight: 600;
            padding: 4px 8px;
            border-radius: 4px;
            display: inline-block;
            min-width: 60px;
            text-align: center;
        }

        /* 대기시간 색상 */
        .wait-green {
            background: rgba(34, 197, 94, 0.2);
            color: #22C55E;
        }

        .wait-blue {
            background: rgba(0, 102, 204, 0.2);
            color: #0066CC;
        }

        .wait-yellow {
            background: rgba(245, 158, 11, 0.2);
            color: #F59E0B;
        }

        .wait-orange {
            background: rgba(251, 146, 60, 0.2);
            color: #FB923C;
        }

        .wait-red {
            background: rgba(239, 68, 68, 0.2);
            color: #EF4444;
        }

        .empty {
            text-align: center;
            padding: 40px;
            color: #64748B;
        }

        .loading {
            text-align: center;
            padding: 40px;
            color: #94A3B8;
        }
    </style>
</head>
<body>
    <div class="header">
        <h1 class="title">미답변 상담</h1>
        <div class="status">
            <div class="status-dot" id="statusDot"></div>
            <span id="statusText">연결 중...</span>
        </div>
    </div>

    <div class="table-container">
        <table>
            <thead>
                <tr>
                    <th>팀</th>
                    <th>담당자</th>
                    <th>마지막 메시지</th>
                    <th>대기시간</th>
                </tr>
            </thead>
            <tbody id="consultationList">
                <tr>
                    <td colspan="4" class="loading">데이터 로딩 중...</td>
                </tr>
            </tbody>
        </table>
    </div>

    <script>
        const CHANNEL_ID = '197228';
        
        class Monitor {
            constructor() {
                this.ws = null;
                this.consultations = [];
                this.reconnectAttempts = 0;
                this.init();
            }

            init() {
                this.connect();
                this.startTimer();
            }

            connect() {
                const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
                const wsUrl = `${protocol}//${window.location.host}/ws`;
                
                try {
                    this.ws = new WebSocket(wsUrl);
                    
                    this.ws.onopen = () => {
                        console.log('WebSocket 연결됨');
                        this.updateStatus(true);
                        this.reconnectAttempts = 0;
                    };

                    this.ws.onmessage = (event) => {
                        const data = JSON.parse(event.data);
                        this.handleMessage(data);
                    };

                    this.ws.onclose = () => {
                        console.log('WebSocket 연결 끊김');
                        this.updateStatus(false);
                        this.reconnect();
                    };

                    this.ws.onerror = (error) => {
                        console.error('WebSocket 에러:', error);
                        this.updateStatus(false);
                    };
                    
                } catch (error) {
                    console.error('WebSocket 연결 실패:', error);
                    this.updateStatus(false);
                    this.reconnect();
                }
            }

            reconnect() {
                if (this.reconnectAttempts < 10) {
                    this.reconnectAttempts++;
                    setTimeout(() => this.connect(), 2000);
                }
            }

            handleMessage(data) {
                if (data.type === 'initial' || data.type === 'update') {
                    this.consultations = data.data || [];
                    this.render();
                }
            }

            render() {
                const tbody = document.getElementById('consultationList');
                
                if (this.consultations.length === 0) {
                    tbody.innerHTML = '<tr><td colspan="4" class="empty">미답변 상담이 없습니다</td></tr>';
                    return;
                }

                tbody.innerHTML = this.consultations.map(c => {
                    const waitClass = this.getWaitClass(c.wait_minutes);
                    
                    return `
                        <tr ondblclick="monitor.openChat('${c.id}')">
                            <td class="team">${c.team}</td>
                            <td class="manager">${c.manager}</td>
                            <td class="message" title="${this.escapeHtml(c.last_message)}">${this.escapeHtml(c.last_message)}</td>
                            <td><span class="wait-time ${waitClass}">${c.wait_minutes}분</span></td>
                        </tr>
                    `;
                }).join('');
            }

            getWaitClass(minutes) {
                if (minutes >= 11) return 'wait-red';
                if (minutes >= 7) return 'wait-orange';
                if (minutes >= 5) return 'wait-yellow';
                if (minutes >= 3) return 'wait-blue';
                return 'wait-green';
            }

            openChat(chatId) {
                window.open(`https://desk.channel.io/#/channels/${CHANNEL_ID}/user_chats/${chatId}`, '_blank');
            }

            updateStatus(connected) {
                const dot = document.getElementById('statusDot');
                const text = document.getElementById('statusText');
                
                if (connected) {
                    dot.classList.remove('disconnected');
                    text.textContent = '연결됨';
                } else {
                    dot.classList.add('disconnected');
                    text.textContent = '연결 끊김';
                }
            }

            escapeHtml(text) {
                const div = document.createElement('div');
                div.textContent = text || '';
                return div.innerHTML;
            }

            startTimer() {
                // 1분마다 대기시간 재계산 및 재렌더링
                setInterval(() => {
                    this.consultations.forEach(c => {
                        const msgTime = new Date(c.last_message_time);
                        const now = new Date();
                        c.wait_minutes = Math.floor((now - msgTime) / 60000);
                    });
                    this.render();
                }, 60000);
            }
        }

        const monitor = new Monitor();
    </script>
</body>
</html>"""
    return HTMLResponse(content=html_content)

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("main:app", host="0.0.0.0", port=port, workers=2)
