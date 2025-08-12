#!/usr/bin/env python3
"""
아정당 채널톡 미답변 상담 실시간 모니터링
Render.com 배포용 - Redis 필수 버전
"""

import asyncio
import json
import logging
import os
import sys
import time
from datetime import datetime, timedelta
from collections import defaultdict
from typing import Dict, List, Optional, Set

import aiohttp
from aiohttp import web
import redis

# 로깅 설정
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# 환경 변수
REDIS_URL = os.getenv("REDIS_URL")
WEBHOOK_TOKEN = os.getenv("CHANNEL_WEBHOOK_TOKEN", "80ab2d11835f44b89010c8efa5eec4b4")
PORT = int(os.getenv("PORT", 10000))

# Redis 필수 체크
if not REDIS_URL:
    logger.error("❌ REDIS_URL 환경 변수가 설정되지 않았습니다!")
    logger.error("Render에서 Redis 서비스를 생성하고 연결하세요.")
    sys.exit(1)

# Chat 클래스
class Chat:
    def __init__(self, chat_id, assignee_name, team, customer_phone, 
                 customer_name=None, message=None, created_at=None):
        self.chat_id = chat_id
        self.assignee_name = assignee_name
        self.team = team
        self.customer_phone = customer_phone
        self.customer_name = customer_name
        self.message = message
        self.created_at = created_at or datetime.now()
        self.last_activity = datetime.now()
    
    def to_dict(self):
        wait_minutes = int((datetime.now() - self.created_at).total_seconds() / 60)
        
        return {
            "chat_id": self.chat_id,
            "assignee_name": self.assignee_name,
            "team": self.team,
            "customer_phone": self.customer_phone,
            "customer_name": self.customer_name,
            "message": self.message,
            "created_at": self.created_at.isoformat(),
            "last_activity": self.last_activity.isoformat(),
            "wait_minutes": wait_minutes,
            "is_urgent": wait_minutes >= 30,
            "priority": "high" if wait_minutes >= 30 else "medium" if wait_minutes >= 10 else "low"
        }
    
    def to_json(self):
        return json.dumps(self.to_dict())
    
    @classmethod
    def from_json(cls, json_str):
        data = json.loads(json_str)
        chat = cls(
            chat_id=data["chat_id"],
            assignee_name=data["assignee_name"],
            team=data["team"],
            customer_phone=data["customer_phone"],
            customer_name=data.get("customer_name"),
            message=data.get("message")
        )
        chat.created_at = datetime.fromisoformat(data["created_at"])
        return chat

# Redis 매니저
class RedisManager:
    def __init__(self):
        self.redis_client = None
        self.connect()
    
    def connect(self):
        """Redis 연결 (필수)"""
        try:
            self.redis_client = redis.from_url(REDIS_URL, decode_responses=True)
            self.redis_client.ping()
            logger.info("✅ Redis 연결 성공!")
        except Exception as e:
            logger.error(f"❌ Redis 연결 실패: {e}")
            logger.error("Redis가 필수입니다. 연결을 확인하세요.")
            sys.exit(1)
    
    def add_chat(self, chat):
        """상담 추가"""
        try:
            # 중복 체크
            if self.redis_client.hexists("chats:waiting", chat.chat_id):
                return False
            
            # 이미 답변된 상담인지 체크
            if self.redis_client.sismember("chats:answered", chat.chat_id):
                return False
            
            # 저장
            self.redis_client.hset("chats:waiting", chat.chat_id, chat.to_json())
            self.redis_client.expire("chats:waiting", 14400)  # 4시간
            
            # 팀 통계 업데이트
            self.redis_client.hincrby("stats:teams", f"{chat.team}:waiting", 1)
            
            logger.info(f"✅ 새 상담 추가: {chat.chat_id}")
            return True
            
        except Exception as e:
            logger.error(f"상담 추가 실패: {e}")
            return False
    
    def remove_chat(self, chat_id, answerer=None):
        """상담 제거"""
        try:
            # 상담 정보 가져오기
            chat_json = self.redis_client.hget("chats:waiting", chat_id)
            if not chat_json:
                return False
            
            chat = Chat.from_json(chat_json)
            
            # 제거
            self.redis_client.hdel("chats:waiting", chat_id)
            
            # 답변 완료 목록에 추가
            self.redis_client.sadd("chats:answered", chat_id)
            self.redis_client.expire("chats:answered", 86400)  # 24시간
            
            # 통계 업데이트
            self.redis_client.hincrby("stats:teams", f"{chat.team}:waiting", -1)
            self.redis_client.hincrby("stats:teams", f"{chat.team}:answered", 1)
            
            if answerer:
                # 매니저 통계
                response_time = (datetime.now() - chat.created_at).total_seconds() / 60
                self.redis_client.hincrby(f"stats:manager:{answerer}", "count", 1)
                self.redis_client.hincrbyfloat(f"stats:manager:{answerer}", "total_time", response_time)
            
            logger.info(f"❌ 상담 제거: {chat_id}")
            return True
            
        except Exception as e:
            logger.error(f"상담 제거 실패: {e}")
            return False
    
    def get_waiting_chats(self, team=None):
        """대기 상담 조회"""
        chats = []
        
        try:
            all_chats = self.redis_client.hgetall("chats:waiting")
            
            for chat_id, chat_json in all_chats.items():
                try:
                    chat = Chat.from_json(chat_json)
                    
                    # 4시간 이상 오래된 상담 자동 제거
                    if (datetime.now() - chat.created_at).total_seconds() > 14400:
                        self.remove_chat(chat_id)
                        continue
                    
                    if not team or chat.team == team:
                        chats.append(chat)
                        
                except Exception as e:
                    logger.error(f"상담 파싱 오류 {chat_id}: {e}")
                    self.redis_client.hdel("chats:waiting", chat_id)
            
        except Exception as e:
            logger.error(f"상담 조회 실패: {e}")
        
        # 대기 시간 기준 정렬 (오래된 것 우선)
        chats.sort(key=lambda x: x.created_at)
        return chats
    
    def get_stats(self):
        """통계 조회"""
        try:
            chats = self.get_waiting_chats()
            
            total = len(chats)
            urgent = len([c for c in chats if c.to_dict()["is_urgent"]])
            avg_wait = 0
            
            if chats:
                total_minutes = sum(c.to_dict()["wait_minutes"] for c in chats)
                avg_wait = total_minutes / len(chats)
            
            # 팀별 통계
            team_stats = defaultdict(lambda: {"waiting": 0, "urgent": 0, "answered": 0})
            
            for chat in chats:
                chat_dict = chat.to_dict()
                team_stats[chat.team]["waiting"] += 1
                if chat_dict["is_urgent"]:
                    team_stats[chat.team]["urgent"] += 1
            
            # Redis에서 답변 통계 가져오기
            team_data = self.redis_client.hgetall("stats:teams")
            for key, value in team_data.items():
                if key.endswith(":answered"):
                    team = key.replace(":answered", "")
                    team_stats[team]["answered"] = int(value)
            
            return {
                "total_waiting": total,
                "urgent_count": urgent,
                "avg_wait_time": round(avg_wait, 1),
                "team_stats": dict(team_stats)
            }
            
        except Exception as e:
            logger.error(f"통계 조회 실패: {e}")
            return {
                "total_waiting": 0,
                "urgent_count": 0,
                "avg_wait_time": 0,
                "team_stats": {}
            }

# WebSocket 관리
class WebSocketManager:
    def __init__(self):
        self.websockets = set()
    
    async def add_connection(self, ws):
        self.websockets.add(ws)
        logger.info(f"🔌 WebSocket 연결 (총 {len(self.websockets)}개)")
    
    def remove_connection(self, ws):
        self.websockets.discard(ws)
        logger.info(f"🔌 WebSocket 해제 (총 {len(self.websockets)}개)")
    
    async def broadcast(self, message):
        """모든 클라이언트에 메시지 전송"""
        if not self.websockets:
            return
        
        message_str = json.dumps(message, default=str)
        disconnected = set()
        
        for ws in self.websockets:
            try:
                await ws.send_str(message_str)
            except Exception:
                disconnected.add(ws)
        
        for ws in disconnected:
            self.remove_connection(ws)

# 전역 인스턴스
redis_manager = RedisManager()
ws_manager = WebSocketManager()

# 웹훅 처리
async def handle_webhook(request):
    """채널톡 웹훅 처리"""
    token = request.query.get("token")
    if token != WEBHOOK_TOKEN:
        return web.json_response({"error": "Invalid token"}, status=403)
    
    try:
        data = await request.json()
        event_type = data.get("type")
        event_data = data.get("data", {})
        
        logger.info(f"📨 웹훅 수신: {event_type}")
        
        if event_type == "message":
            person_type = event_data.get("personType")
            chat_id = event_data.get("userChat", {}).get("id")
            
            if person_type == "user" and chat_id:
                # 새 상담
                user_chat = event_data.get("userChat", {})
                assignee = user_chat.get("assignee", {})
                user = user_chat.get("user", {})
                profile = user.get("profile", {})
                teams = assignee.get("teams", [])
                
                chat = Chat(
                    chat_id=chat_id,
                    assignee_name=assignee.get("name", "미배정"),
                    team=teams[0].get("name") if teams else "미배정",
                    customer_phone=profile.get("mobileNumber", profile.get("name", "Unknown")),
                    customer_name=profile.get("name"),
                    message=event_data.get("message", {}).get("plainText")
                )
                
                if redis_manager.add_chat(chat):
                    await ws_manager.broadcast({
                        "type": "new_chat",
                        "chat": chat.to_dict()
                    })
            
            elif person_type in ["manager", "bot"] and chat_id:
                # 답변 완료
                answerer = event_data.get("person", {}).get("name")
                if redis_manager.remove_chat(chat_id, answerer):
                    await ws_manager.broadcast({
                        "type": "chat_answered",
                        "chat_id": chat_id
                    })
        
        return web.json_response({"status": "success"})
        
    except Exception as e:
        logger.error(f"웹훅 처리 오류: {e}")
        return web.json_response({"error": str(e)}, status=500)

# API 엔드포인트
async def handle_get_chats(request):
    """상담 목록 조회"""
    try:
        team = request.query.get("team")
        chats = redis_manager.get_waiting_chats(team)
        stats = redis_manager.get_stats()
        
        return web.json_response({
            "chats": [chat.to_dict() for chat in chats],
            "stats": stats,
            "timestamp": datetime.now().isoformat()
        })
    except Exception as e:
        logger.error(f"상담 조회 오류: {e}")
        return web.json_response({"error": str(e)}, status=500)

async def handle_stats(request):
    """통계 조회"""
    try:
        stats = redis_manager.get_stats()
        return web.json_response(stats)
    except Exception as e:
        logger.error(f"통계 조회 오류: {e}")
        return web.json_response({"error": str(e)}, status=500)

async def handle_health(request):
    """헬스체크"""
    try:
        redis_manager.redis_client.ping()
        redis_status = "healthy"
    except Exception as e:
        redis_status = f"unhealthy: {e}"
    
    return web.json_response({
        "status": "healthy" if redis_status == "healthy" else "unhealthy",
        "timestamp": datetime.now().isoformat(),
        "connections": len(ws_manager.websockets),
        "redis": redis_status
    })

async def handle_websocket(request):
    """WebSocket 연결"""
    ws = web.WebSocketResponse()
    await ws.prepare(request)
    await ws_manager.add_connection(ws)
    
    try:
        # 초기 데이터 전송
        chats = redis_manager.get_waiting_chats()
        await ws.send_json({
            "type": "initial_data",
            "chats": [chat.to_dict() for chat in chats]
        })
        
        # 메시지 수신 대기
        async for msg in ws:
            if msg.type == aiohttp.WSMsgType.TEXT:
                if msg.data == "ping":
                    await ws.send_str("pong")
            elif msg.type == aiohttp.WSMsgType.ERROR:
                logger.error(f'WebSocket error: {ws.exception()}')
    
    except Exception as e:
        logger.error(f"WebSocket 오류: {e}")
    finally:
        ws_manager.remove_connection(ws)
    
    return ws

async def handle_index(request):
    """메인 페이지"""
    html = """<!DOCTYPE html>
<html lang="ko">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>아정당 채널톡 모니터링</title>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            min-height: 100vh;
            padding: 20px;
        }
        .container {
            max-width: 1600px;
            margin: 0 auto;
        }
        .header {
            background: rgba(255, 255, 255, 0.95);
            border-radius: 20px;
            padding: 20px 30px;
            margin-bottom: 20px;
            box-shadow: 0 10px 30px rgba(0, 0, 0, 0.1);
        }
        .logo {
            font-size: 24px;
            font-weight: bold;
            background: linear-gradient(135deg, #667eea, #764ba2);
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
            text-align: center;
            margin-bottom: 20px;
        }
        .stats {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
            gap: 20px;
        }
        .stat-card {
            text-align: center;
            padding: 15px;
            background: rgba(255, 255, 255, 0.5);
            border-radius: 10px;
        }
        .stat-number {
            font-size: 32px;
            font-weight: bold;
            color: #333;
        }
        .stat-label {
            font-size: 12px;
            color: #666;
            margin-top: 5px;
        }
        .filters {
            background: white;
            border-radius: 15px;
            padding: 15px;
            margin-bottom: 20px;
            display: flex;
            gap: 15px;
            flex-wrap: wrap;
        }
        select, input {
            padding: 8px 12px;
            border: 1px solid #ddd;
            border-radius: 8px;
            font-size: 14px;
        }
        .chat-grid {
            display: grid;
            grid-template-columns: repeat(auto-fill, minmax(380px, 1fr));
            gap: 15px;
        }
        .chat-card {
            background: white;
            border-radius: 15px;
            padding: 20px;
            box-shadow: 0 4px 15px rgba(0, 0, 0, 0.08);
            transition: transform 0.3s;
        }
        .chat-card:hover {
            transform: translateY(-5px);
        }
        .chat-card.urgent {
            border-left: 4px solid #ff6b6b;
            animation: pulse 2s infinite;
        }
        @keyframes pulse {
            0%, 100% { opacity: 1; }
            50% { opacity: 0.8; }
        }
        .chat-header {
            display: flex;
            justify-content: space-between;
            margin-bottom: 15px;
        }
        .customer-phone {
            font-size: 16px;
            font-weight: 600;
            color: #333;
        }
        .wait-time {
            font-size: 14px;
            color: #ff6b6b;
            font-weight: bold;
        }
        .message {
            padding: 10px;
            background: #f8f9fa;
            border-radius: 8px;
            margin-bottom: 15px;
            min-height: 50px;
            font-size: 13px;
            color: #555;
        }
        .assignee {
            display: flex;
            align-items: center;
            gap: 10px;
            padding: 8px;
            background: #f0f2f5;
            border-radius: 8px;
        }
        .assignee-avatar {
            width: 30px;
            height: 30px;
            border-radius: 50%;
            background: linear-gradient(135deg, #667eea, #764ba2);
            display: flex;
            align-items: center;
            justify-content: center;
            color: white;
            font-weight: bold;
            font-size: 12px;
        }
        .connection-status {
            position: fixed;
            top: 20px;
            right: 20px;
            background: white;
            padding: 10px 20px;
            border-radius: 30px;
            box-shadow: 0 4px 15px rgba(0, 0, 0, 0.1);
            display: flex;
            align-items: center;
            gap: 10px;
            z-index: 1000;
        }
        .status-dot {
            width: 8px;
            height: 8px;
            border-radius: 50%;
            animation: blink 1s infinite;
        }
        .status-dot.connected { background: #4caf50; }
        .status-dot.disconnected { background: #f44336; }
        @keyframes blink {
            0%, 100% { opacity: 1; }
            50% { opacity: 0.3; }
        }
        .empty-state {
            grid-column: 1 / -1;
            text-align: center;
            padding: 60px;
            background: white;
            border-radius: 15px;
        }
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <div class="logo">🎯 아정당 채널톡 모니터링</div>
            <div class="stats" id="stats"></div>
        </div>
        
        <div class="filters">
            <select id="teamFilter">
                <option value="">전체 팀</option>
                <option value="SNS 1팀">SNS 1팀</option>
                <option value="SNS 2팀">SNS 2팀</option>
                <option value="SNS 3팀">SNS 3팀</option>
                <option value="SNS 4팀">SNS 4팀</option>
                <option value="의정부 SNS팀">의정부 SNS팀</option>
                <option value="미배정">미배정</option>
            </select>
            <input type="text" id="searchInput" placeholder="전화번호 검색">
        </div>
        
        <div class="connection-status">
            <div class="status-dot" id="statusDot"></div>
            <span id="statusText">연결 중...</span>
        </div>
        
        <div class="chat-grid" id="chatGrid">
            <div class="empty-state">데이터 로딩 중...</div>
        </div>
    </div>
    
    <script>
        let ws = null;
        let chats = [];
        
        function formatWaitTime(minutes) {
            if (minutes < 1) return '방금';
            if (minutes < 60) return minutes + '분';
            const hours = Math.floor(minutes / 60);
            const mins = minutes % 60;
            return hours + '시간 ' + mins + '분';
        }
        
        function connectWebSocket() {
            const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
            ws = new WebSocket(protocol + '//' + window.location.host + '/ws');
            
            ws.onopen = () => {
                document.getElementById('statusDot').className = 'status-dot connected';
                document.getElementById('statusText').textContent = '실시간 연결됨';
            };
            
            ws.onmessage = (event) => {
                const data = JSON.parse(event.data);
                
                if (data.type === 'initial_data') {
                    chats = data.chats || [];
                    renderChats();
                } else if (data.type === 'new_chat') {
                    fetchChats();
                } else if (data.type === 'chat_answered') {
                    fetchChats();
                }
            };
            
            ws.onerror = ws.onclose = () => {
                document.getElementById('statusDot').className = 'status-dot disconnected';
                document.getElementById('statusText').textContent = '재연결 중...';
                setTimeout(connectWebSocket, 5000);
            };
            
            setInterval(() => {
                if (ws.readyState === WebSocket.OPEN) {
                    ws.send('ping');
                }
            }, 30000);
        }
        
        async function fetchChats() {
            try {
                const response = await fetch('/api/chats');
                const data = await response.json();
                chats = data.chats || [];
                
                // 통계 업데이트
                const stats = data.stats;
                document.getElementById('stats').innerHTML = `
                    <div class="stat-card">
                        <div class="stat-number">${stats.total_waiting}</div>
                        <div class="stat-label">전체 대기</div>
                    </div>
                    <div class="stat-card">
                        <div class="stat-number">${stats.urgent_count}</div>
                        <div class="stat-label">긴급 응답</div>
                    </div>
                    <div class="stat-card">
                        <div class="stat-number">${stats.avg_wait_time}분</div>
                        <div class="stat-label">평균 대기</div>
                    </div>
                `;
                
                renderChats();
            } catch (error) {
                console.error('데이터 로드 실패:', error);
            }
        }
        
        function renderChats() {
            const teamFilter = document.getElementById('teamFilter').value;
            const searchQuery = document.getElementById('searchInput').value.toLowerCase();
            
            let filteredChats = chats;
            
            if (teamFilter) {
                filteredChats = filteredChats.filter(c => c.team === teamFilter);
            }
            
            if (searchQuery) {
                filteredChats = filteredChats.filter(c => 
                    c.customer_phone.toLowerCase().includes(searchQuery)
                );
            }
            
            const grid = document.getElementById('chatGrid');
            
            if (filteredChats.length === 0) {
                grid.innerHTML = '<div class="empty-state"><div style="font-size: 48px;">🎉</div><h2>모든 상담이 처리되었습니다!</h2></div>';
            } else {
                grid.innerHTML = filteredChats.map(chat => `
                    <div class="chat-card ${chat.is_urgent ? 'urgent' : ''}">
                        <div class="chat-header">
                            <div class="customer-phone">${chat.customer_phone}</div>
                            <div class="wait-time">${formatWaitTime(chat.wait_minutes)}</div>
                        </div>
                        <div class="message">${chat.message || '(메시지 없음)'}</div>
                        <div class="assignee">
                            <div class="assignee-avatar">${chat.assignee_name.charAt(0)}</div>
                            <div>
                                <div style="font-weight: 600;">${chat.assignee_name}</div>
                                <div style="font-size: 12px; color: #666;">${chat.team}</div>
                            </div>
                        </div>
                    </div>
                `).join('');
            }
        }
        
        // 초기화
        connectWebSocket();
        fetchChats();
        setInterval(fetchChats, 10000);
        
        document.getElementById('teamFilter').addEventListener('change', renderChats);
        document.getElementById('searchInput').addEventListener('input', renderChats);
    </script>
</body>
</html>"""
    return web.Response(text=html, content_type='text/html')

# 주기적 정리
async def periodic_cleanup(app):
    """오래된 데이터 정리"""
    while True:
        try:
            await asyncio.sleep(3600)  # 1시간마다
            
            chats = redis_manager.get_waiting_chats()
            threshold = datetime.now() - timedelta(hours=4)
            
            for chat in chats:
                if chat.created_at < threshold:
                    redis_manager.remove_chat(chat.chat_id)
                    logger.info(f"🧹 오래된 상담 제거: {chat.chat_id}")
            
        except Exception as e:
            logger.error(f"정리 작업 오류: {e}")

# 앱 생성
def create_app():
    app = web.Application()
    
    # 라우트
    app.router.add_get('/', handle_index)
    app.router.add_post('/webhook', handle_webhook)
    app.router.add_get('/api/chats', handle_get_chats)
    app.router.add_get('/api/stats', handle_stats)
    app.router.add_get('/health', handle_health)
    app.router.add_get('/ws', handle_websocket)
    
    # 백그라운드 태스크
    async def start_background_tasks(app):
        app['cleanup_task'] = asyncio.create_task(periodic_cleanup(app))
    
    async def cleanup_background_tasks(app):
        app['cleanup_task'].cancel()
        await app['cleanup_task']
    
    app.on_startup.append(start_background_tasks)
    app.on_cleanup.append(cleanup_background_tasks)
    
    return app

# 메인
if __name__ == '__main__':
    logger.info(f"🚀 서버 시작 - 포트 {PORT}")
    logger.info(f"📍 Redis URL: {REDIS_URL}")
    app = create_app()
    web.run_app(app, host='0.0.0.0', port=PORT)
