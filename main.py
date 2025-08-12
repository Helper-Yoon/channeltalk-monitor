#!/usr/bin/env python3
"""
아정당 채널톡 미답변 상담 실시간 모니터링
실제 작동 검증된 버전 - Redis 필수
"""

import asyncio
import json
import logging
import os
import sys
import time
from datetime import datetime, timedelta
from collections import defaultdict

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
    sys.exit(1)

# Redis 매니저
class RedisManager:
    def __init__(self):
        self.redis_client = None
        self.connect()
    
    def connect(self):
        """Redis 연결"""
        try:
            self.redis_client = redis.from_url(REDIS_URL, decode_responses=True)
            self.redis_client.ping()
            logger.info("✅ Redis 연결 성공!")
        except Exception as e:
            logger.error(f"❌ Redis 연결 실패: {e}")
            sys.exit(1)
    
    def add_chat(self, chat_data):
        """상담 추가"""
        try:
            chat_id = chat_data.get("chat_id")
            if not chat_id:
                return False
            
            # 중복 체크
            if self.redis_client.hexists("chats:waiting", chat_id):
                logger.info(f"⚠️ 이미 존재하는 상담: {chat_id}")
                return False
            
            # 답변된 상담 체크
            if self.redis_client.sismember("chats:answered", chat_id):
                logger.info(f"⚠️ 이미 답변된 상담: {chat_id}")
                return False
            
            # 타임스탬프 추가
            chat_data["created_at"] = datetime.now().isoformat()
            chat_data["last_activity"] = datetime.now().isoformat()
            
            # 저장
            self.redis_client.hset("chats:waiting", chat_id, json.dumps(chat_data))
            self.redis_client.expire("chats:waiting", 14400)  # 4시간
            
            logger.info(f"✅ 새 상담 추가: {chat_id} - {chat_data.get('customer_phone')}")
            return True
            
        except Exception as e:
            logger.error(f"상담 추가 실패: {e}")
            return False
    
    def remove_chat(self, chat_id):
        """상담 제거"""
        try:
            if not chat_id:
                return False
            
            # 대기 목록에서 제거
            result = self.redis_client.hdel("chats:waiting", chat_id)
            
            if result:
                # 답변 완료 목록에 추가
                self.redis_client.sadd("chats:answered", chat_id)
                self.redis_client.expire("chats:answered", 86400)  # 24시간
                logger.info(f"❌ 상담 제거: {chat_id}")
                return True
            
            return False
            
        except Exception as e:
            logger.error(f"상담 제거 실패: {e}")
            return False
    
    def get_waiting_chats(self):
        """대기 상담 조회"""
        chats = []
        
        try:
            all_chats = self.redis_client.hgetall("chats:waiting")
            
            for chat_id, chat_json in all_chats.items():
                try:
                    chat = json.loads(chat_json)
                    
                    # 생성 시간 계산
                    if "created_at" in chat:
                        created = datetime.fromisoformat(chat["created_at"])
                        wait_minutes = int((datetime.now() - created).total_seconds() / 60)
                        chat["wait_minutes"] = wait_minutes
                        chat["is_urgent"] = wait_minutes >= 30
                        chat["priority"] = "high" if wait_minutes >= 30 else "medium" if wait_minutes >= 10 else "low"
                    
                    chats.append(chat)
                    
                except Exception as e:
                    logger.error(f"상담 파싱 오류 {chat_id}: {e}")
                    self.redis_client.hdel("chats:waiting", chat_id)
            
        except Exception as e:
            logger.error(f"상담 조회 실패: {e}")
        
        # 대기 시간 기준 정렬
        chats.sort(key=lambda x: x.get("wait_minutes", 0), reverse=True)
        return chats
    
    def get_stats(self):
        """통계 조회"""
        try:
            chats = self.get_waiting_chats()
            
            total = len(chats)
            urgent = len([c for c in chats if c.get("is_urgent", False)])
            avg_wait = 0
            
            if chats:
                total_minutes = sum(c.get("wait_minutes", 0) for c in chats)
                avg_wait = total_minutes / len(chats)
            
            # 팀별 통계
            team_stats = defaultdict(lambda: {"waiting": 0, "urgent": 0})
            
            for chat in chats:
                team = chat.get("team", "미배정")
                team_stats[team]["waiting"] += 1
                if chat.get("is_urgent", False):
                    team_stats[team]["urgent"] += 1
            
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
    # 토큰 검증
    token = request.query.get("token")
    if token != WEBHOOK_TOKEN:
        return web.json_response({"error": "Invalid token"}, status=403)
    
    try:
        body = await request.json()
        
        # 디버깅용 로그
        logger.info(f"📨 웹훅 수신: {body.get('type', 'unknown')}")
        
        # refers 객체에서 이벤트 타입 확인
        refers = body.get("refers", {})
        event_type = refers.get("type", body.get("type"))
        
        # entity 객체에서 데이터 추출 (채널톡 v5 API 형식)
        entity = body.get("entity", body.get("data", {}))
        
        # 메시지 이벤트 처리
        if "message" in event_type.lower() or event_type == "message":
            # personType 확인
            person_type = entity.get("personType", refers.get("personType"))
            
            # userChat 정보
            user_chat = entity.get("userChat", refers.get("userChat", {}))
            chat_id = user_chat.get("id")
            
            if person_type == "user" and chat_id:
                # 사용자 메시지 - 새 상담 또는 업데이트
                assignee = user_chat.get("assignee", {})
                user = user_chat.get("user", {})
                profile = user.get("profile", {})
                teams = assignee.get("teams", [])
                
                # 전화번호 추출 (여러 필드 체크)
                phone = (profile.get("mobileNumber") or 
                        profile.get("phoneNumber") or 
                        profile.get("name") or 
                        user.get("name") or 
                        "Unknown")
                
                # 메시지 추출
                message_obj = entity.get("message", {})
                message_text = (message_obj.get("plainText") or 
                              message_obj.get("text") or 
                              "(메시지 없음)")
                
                chat_data = {
                    "chat_id": chat_id,
                    "assignee_name": assignee.get("name", "미배정"),
                    "assignee_id": assignee.get("id"),
                    "team": teams[0].get("name") if teams else "미배정",
                    "customer_phone": phone,
                    "customer_name": profile.get("name"),
                    "message": message_text
                }
                
                if redis_manager.add_chat(chat_data):
                    await ws_manager.broadcast({
                        "type": "new_chat",
                        "chat": chat_data
                    })
            
            elif person_type in ["manager", "bot"] and chat_id:
                # 매니저/봇 응답 - 상담 제거
                if redis_manager.remove_chat(chat_id):
                    await ws_manager.broadcast({
                        "type": "chat_answered",
                        "chat_id": chat_id
                    })
        
        # userChat 이벤트 처리 (상태 변경 등)
        elif "userChat" in event_type:
            chat_id = entity.get("id", refers.get("id"))
            state = entity.get("state", refers.get("state"))
            
            # closed 상태면 제거
            if state == "closed" and chat_id:
                if redis_manager.remove_chat(chat_id):
                    await ws_manager.broadcast({
                        "type": "chat_closed",
                        "chat_id": chat_id
                    })
        
        return web.json_response({"status": "success"})
        
    except Exception as e:
        logger.error(f"웹훅 처리 오류: {e}")
        return web.json_response({"status": "error", "message": str(e)}, status=200)

# API 엔드포인트
async def handle_get_chats(request):
    """상담 목록 조회"""
    try:
        chats = redis_manager.get_waiting_chats()
        stats = redis_manager.get_stats()
        
        return web.json_response({
            "chats": chats,
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
            "chats": chats
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
    """메인 페이지 - 다크모드 디자인"""
    html = """<!DOCTYPE html>
<html lang="ko">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>아정당 채널톡 모니터링</title>
    <style>
        * { 
            margin: 0; 
            padding: 0; 
            box-sizing: border-box; 
        }
        
        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            background: #0f0f0f;
            color: #e0e0e0;
            min-height: 100vh;
            padding: 20px;
        }
        
        .container {
            max-width: 1600px;
            margin: 0 auto;
        }
        
        .header {
            background: #1a1a1a;
            border: 1px solid #2a2a2a;
            border-radius: 12px;
            padding: 20px 30px;
            margin-bottom: 20px;
            display: flex;
            justify-content: space-between;
            align-items: center;
        }
        
        .logo {
            font-size: 20px;
            font-weight: 600;
            color: #fff;
            display: flex;
            align-items: center;
            gap: 10px;
        }
        
        .stats {
            display: flex;
            gap: 40px;
        }
        
        .stat-item {
            text-align: center;
        }
        
        .stat-number {
            font-size: 28px;
            font-weight: bold;
            color: #fff;
        }
        
        .stat-number.urgent {
            color: #ff6b6b;
        }
        
        .stat-label {
            font-size: 11px;
            color: #888;
            margin-top: 4px;
            text-transform: uppercase;
            letter-spacing: 0.5px;
        }
        
        .filters {
            background: #1a1a1a;
            border: 1px solid #2a2a2a;
            border-radius: 12px;
            padding: 15px 20px;
            margin-bottom: 20px;
            display: flex;
            gap: 15px;
            align-items: center;
        }
        
        select, input {
            background: #0f0f0f;
            color: #e0e0e0;
            border: 1px solid #333;
            padding: 8px 12px;
            border-radius: 6px;
            font-size: 13px;
            outline: none;
            transition: all 0.2s;
        }
        
        select:focus, input:focus {
            border-color: #4a9eff;
            box-shadow: 0 0 0 2px rgba(74, 158, 255, 0.1);
        }
        
        .chat-grid {
            display: grid;
            grid-template-columns: repeat(auto-fill, minmax(360px, 1fr));
            gap: 15px;
        }
        
        .chat-card {
            background: #1a1a1a;
            border: 1px solid #2a2a2a;
            border-radius: 12px;
            padding: 16px;
            transition: all 0.2s;
            position: relative;
            overflow: hidden;
        }
        
        .chat-card:hover {
            border-color: #333;
            transform: translateY(-2px);
            box-shadow: 0 4px 12px rgba(0, 0, 0, 0.5);
        }
        
        .chat-card.urgent {
            border-left: 3px solid #ff6b6b;
        }
        
        .chat-card.urgent::before {
            content: '';
            position: absolute;
            top: 0;
            left: 0;
            right: 0;
            height: 1px;
            background: linear-gradient(90deg, #ff6b6b, transparent);
            animation: pulse 2s infinite;
        }
        
        @keyframes pulse {
            0%, 100% { opacity: 0.5; }
            50% { opacity: 1; }
        }
        
        .chat-header {
            display: flex;
            justify-content: space-between;
            align-items: flex-start;
            margin-bottom: 12px;
        }
        
        .customer-info {
            flex: 1;
        }
        
        .customer-phone {
            font-size: 15px;
            font-weight: 600;
            color: #fff;
            margin-bottom: 2px;
        }
        
        .customer-name {
            font-size: 12px;
            color: #888;
        }
        
        .wait-time {
            text-align: right;
        }
        
        .wait-time-value {
            font-size: 14px;
            font-weight: 600;
            color: #ff6b6b;
        }
        
        .wait-time-label {
            font-size: 10px;
            color: #666;
            text-transform: uppercase;
        }
        
        .message {
            background: #0f0f0f;
            border: 1px solid #222;
            border-radius: 8px;
            padding: 10px;
            margin-bottom: 12px;
            min-height: 45px;
            font-size: 13px;
            color: #ccc;
            line-height: 1.4;
        }
        
        .message.empty {
            color: #666;
            font-style: italic;
        }
        
        .assignee {
            display: flex;
            align-items: center;
            gap: 10px;
        }
        
        .assignee-avatar {
            width: 28px;
            height: 28px;
            border-radius: 50%;
            background: linear-gradient(135deg, #4a9eff, #00d4ff);
            display: flex;
            align-items: center;
            justify-content: center;
            color: #fff;
            font-weight: bold;
            font-size: 12px;
        }
        
        .assignee-details {
            flex: 1;
        }
        
        .assignee-name {
            font-size: 13px;
            font-weight: 500;
            color: #fff;
        }
        
        .team-name {
            font-size: 11px;
            color: #666;
        }
        
        .priority-badge {
            padding: 2px 8px;
            border-radius: 4px;
            font-size: 10px;
            font-weight: 600;
            text-transform: uppercase;
            letter-spacing: 0.5px;
            float: right;
        }
        
        .priority-high {
            background: rgba(255, 107, 107, 0.2);
            color: #ff6b6b;
            border: 1px solid rgba(255, 107, 107, 0.3);
        }
        
        .priority-medium {
            background: rgba(255, 193, 7, 0.2);
            color: #ffc107;
            border: 1px solid rgba(255, 193, 7, 0.3);
        }
        
        .priority-low {
            background: rgba(76, 175, 80, 0.2);
            color: #4caf50;
            border: 1px solid rgba(76, 175, 80, 0.3);
        }
        
        .connection-status {
            position: fixed;
            top: 20px;
            right: 20px;
            background: #1a1a1a;
            border: 1px solid #2a2a2a;
            padding: 8px 16px;
            border-radius: 20px;
            display: flex;
            align-items: center;
            gap: 8px;
            font-size: 12px;
            z-index: 1000;
        }
        
        .status-dot {
            width: 6px;
            height: 6px;
            border-radius: 50%;
        }
        
        .status-dot.connected {
            background: #4caf50;
            box-shadow: 0 0 4px #4caf50;
            animation: blink 2s infinite;
        }
        
        .status-dot.disconnected {
            background: #f44336;
            box-shadow: 0 0 4px #f44336;
        }
        
        @keyframes blink {
            0%, 100% { opacity: 1; }
            50% { opacity: 0.5; }
        }
        
        .empty-state {
            grid-column: 1 / -1;
            text-align: center;
            padding: 80px 20px;
            background: #1a1a1a;
            border: 1px solid #2a2a2a;
            border-radius: 12px;
        }
        
        .empty-icon {
            font-size: 64px;
            margin-bottom: 20px;
            opacity: 0.5;
        }
        
        .empty-title {
            font-size: 20px;
            color: #fff;
            margin-bottom: 8px;
        }
        
        .empty-desc {
            font-size: 14px;
            color: #666;
        }
        
        @media (max-width: 768px) {
            .stats {
                gap: 20px;
            }
            
            .stat-number {
                font-size: 24px;
            }
            
            .chat-grid {
                grid-template-columns: 1fr;
            }
        }
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <div class="logo">
                <span>🎯</span>
                <span>아정당 채널톡 모니터링</span>
            </div>
            <div class="stats">
                <div class="stat-item">
                    <div class="stat-number" id="totalCount">0</div>
                    <div class="stat-label">전체 대기</div>
                </div>
                <div class="stat-item">
                    <div class="stat-number urgent" id="urgentCount">0</div>
                    <div class="stat-label">긴급 응답</div>
                </div>
                <div class="stat-item">
                    <div class="stat-number" id="avgWaitTime">0분</div>
                    <div class="stat-label">평균 대기</div>
                </div>
            </div>
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
            <input type="text" id="searchInput" placeholder="전화번호 또는 이름 검색...">
        </div>
        
        <div class="connection-status">
            <div class="status-dot disconnected" id="statusDot"></div>
            <span id="statusText">연결 중...</span>
        </div>
        
        <div class="chat-grid" id="chatGrid">
            <div class="empty-state">
                <div class="empty-icon">⏳</div>
                <div class="empty-title">데이터 로딩 중...</div>
                <div class="empty-desc">잠시만 기다려주세요</div>
            </div>
        </div>
    </div>
    
    <script>
        let ws = null;
        let chats = [];
        let reconnectTimer = null;
        
        function formatWaitTime(minutes) {
            if (!minutes || minutes < 1) return '방금';
            if (minutes < 60) return minutes + '분';
            const hours = Math.floor(minutes / 60);
            const mins = minutes % 60;
            return hours + '시간 ' + (mins > 0 ? mins + '분' : '');
        }
        
        function getPriorityClass(priority) {
            return 'priority-' + (priority || 'low');
        }
        
        function getPriorityText(priority) {
            const map = {
                'high': '긴급',
                'medium': '보통',
                'low': '일반'
            };
            return map[priority] || '일반';
        }
        
        function connectWebSocket() {
            const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
            const wsUrl = protocol + '//' + window.location.host + '/ws';
            
            ws = new WebSocket(wsUrl);
            
            ws.onopen = () => {
                console.log('WebSocket 연결됨');
                document.getElementById('statusDot').className = 'status-dot connected';
                document.getElementById('statusText').textContent = '실시간 연결됨';
                
                if (reconnectTimer) {
                    clearTimeout(reconnectTimer);
                    reconnectTimer = null;
                }
            };
            
            ws.onmessage = (event) => {
                const data = JSON.parse(event.data);
                
                if (data.type === 'initial_data') {
                    chats = data.chats || [];
                    renderChats();
                } else if (data.type === 'new_chat' || data.type === 'chat_answered' || data.type === 'chat_closed') {
                    // 데이터 새로고침
                    fetchChats();
                }
            };
            
            ws.onerror = (error) => {
                console.error('WebSocket 오류:', error);
            };
            
            ws.onclose = () => {
                console.log('WebSocket 연결 끊김');
                document.getElementById('statusDot').className = 'status-dot disconnected';
                document.getElementById('statusText').textContent = '재연결 중...';
                
                // 5초 후 재연결
                if (!reconnectTimer) {
                    reconnectTimer = setTimeout(() => {
                        reconnectTimer = null;
                        connectWebSocket();
                    }, 5000);
                }
            };
            
            // 30초마다 핑
            setInterval(() => {
                if (ws && ws.readyState === WebSocket.OPEN) {
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
                const stats = data.stats || {};
                document.getElementById('totalCount').textContent = stats.total_waiting || 0;
                document.getElementById('urgentCount').textContent = stats.urgent_count || 0;
                document.getElementById('avgWaitTime').textContent = 
                    (stats.avg_wait_time ? Math.round(stats.avg_wait_time) + '분' : '0분');
                
                renderChats();
                
            } catch (error) {
                console.error('데이터 로드 실패:', error);
            }
        }
        
        function renderChats() {
            const teamFilter = document.getElementById('teamFilter').value;
            const searchQuery = document.getElementById('searchInput').value.toLowerCase();
            
            let filteredChats = [...chats];
            
            // 팀 필터
            if (teamFilter) {
                filteredChats = filteredChats.filter(c => c.team === teamFilter);
            }
            
            // 검색 필터
            if (searchQuery) {
                filteredChats = filteredChats.filter(c => {
                    const phone = (c.customer_phone || '').toLowerCase();
                    const name = (c.customer_name || '').toLowerCase();
                    const assignee = (c.assignee_name || '').toLowerCase();
                    return phone.includes(searchQuery) || 
                           name.includes(searchQuery) || 
                           assignee.includes(searchQuery);
                });
            }
            
            const grid = document.getElementById('chatGrid');
            
            if (filteredChats.length === 0) {
                grid.innerHTML = `
                    <div class="empty-state">
                        <div class="empty-icon">✨</div>
                        <div class="empty-title">모든 상담이 처리되었습니다!</div>
                        <div class="empty-desc">현재 대기 중인 상담이 없습니다</div>
                    </div>
                `;
            } else {
                grid.innerHTML = filteredChats.map(chat => {
                    const isUrgent = chat.is_urgent || chat.wait_minutes >= 30;
                    const priority = chat.priority || (isUrgent ? 'high' : chat.wait_minutes >= 10 ? 'medium' : 'low');
                    
                    return `
                        <div class="chat-card ${isUrgent ? 'urgent' : ''}">
                            <div class="chat-header">
                                <div class="customer-info">
                                    <div class="customer-phone">${chat.customer_phone || 'Unknown'}</div>
                                    ${chat.customer_name ? `<div class="customer-name">${chat.customer_name}</div>` : ''}
                                </div>
                                <div class="wait-time">
                                    <div class="wait-time-value">${formatWaitTime(chat.wait_minutes)}</div>
                                    <div class="wait-time-label">대기시간</div>
                                </div>
                            </div>
                            <div class="message ${!chat.message || chat.message === '(메시지 없음)' ? 'empty' : ''}">
                                ${chat.message || '(메시지 없음)'}
                            </div>
                            <div class="assignee">
                                <div class="assignee-avatar">
                                    ${(chat.assignee_name || '?').charAt(0).toUpperCase()}
                                </div>
                                <div class="assignee-details">
                                    <div class="assignee-name">
                                        ${chat.assignee_name || '미배정'}
                                        <span class="${getPriorityClass(priority)} priority-badge">
                                            ${getPriorityText(priority)}
                                        </span>
                                    </div>
                                    <div class="team-name">${chat.team || '미배정'}</div>
                                </div>
                            </div>
                        </div>
                    `;
                }).join('');
            }
        }
        
        // 초기화
        document.addEventListener('DOMContentLoaded', () => {
            // WebSocket 연결
            connectWebSocket();
            
            // 초기 데이터 로드
            fetchChats();
            
            // 10초마다 새로고침 (폴백)
            setInterval(fetchChats, 10000);
            
            // 필터 이벤트
            document.getElementById('teamFilter').addEventListener('change', renderChats);
            document.getElementById('searchInput').addEventListener('input', renderChats);
            
            // 알림 권한 요청
            if ('Notification' in window && Notification.permission === 'default') {
                Notification.requestPermission();
            }
        });
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
                created_str = chat.get("created_at")
                if created_str:
                    created = datetime.fromisoformat(created_str)
                    if created < threshold:
                        redis_manager.remove_chat(chat.get("chat_id"))
                        logger.info(f"🧹 오래된 상담 제거: {chat.get('chat_id')}")
            
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
