import asyncio
import aiohttp
from aiohttp import web
import redis.asyncio as aioredis
import json
import os
from datetime import datetime
import logging
from typing import Dict, List, Optional
import weakref

# ===== 환경 변수 =====
REDIS_URL = os.getenv('REDIS_URL', 'redis://red-d2ct46buibrs738rintg:6379')
WEBHOOK_TOKEN = os.getenv('WEBHOOK_TOKEN', '80ab2d11835f44b89010c8efa5eec4b4')
PORT = int(os.getenv('PORT', 10000))

# ===== 로깅 설정 =====
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

class ChannelTalkMonitor:
    """Redis를 제대로 활용하는 채널톡 모니터링"""
    
    def __init__(self):
        self.redis: Optional[aioredis.Redis] = None
        self.websockets = weakref.WeakSet()
        logger.info("🚀 ChannelTalkMonitor 시작 - Redis 중심 모드")
        
    async def setup(self):
        """Redis 연결 - 실패하면 죽는다"""
        max_retries = 5
        retry_count = 0
        
        while retry_count < max_retries:
            try:
                # Redis 연결 (제대로!)
                self.redis = aioredis.from_url(
                    REDIS_URL,
                    decode_responses=True,
                    health_check_interval=30,
                    socket_connect_timeout=5,
                    retry_on_timeout=True,
                    socket_keepalive=True
                )
                
                # 연결 테스트
                await self.redis.ping()
                logger.info(f"✅ Redis 연결 성공! (시도 {retry_count + 1})")
                
                # Redis 정보 출력
                info = await self.redis.info()
                logger.info(f"📊 Redis 정보: 메모리 사용 {info.get('used_memory_human', 'N/A')}")
                
                # 기존 데이터 개수 확인
                existing_count = await self.redis.scard('unanswered_chats')
                logger.info(f"📥 기존 미답변 상담: {existing_count}개")
                
                return
                
            except Exception as e:
                retry_count += 1
                logger.error(f"❌ Redis 연결 실패 (시도 {retry_count}/{max_retries}): {e}")
                if retry_count < max_retries:
                    await asyncio.sleep(2)
                else:
                    raise Exception("Redis 연결 불가능! 서비스를 시작할 수 없습니다.")
    
    async def cleanup(self):
        """종료시 정리"""
        if self.redis:
            await self.redis.close()
            logger.info("👋 Redis 연결 종료")
    
    async def save_chat(self, chat_data: dict):
        """Redis에 저장 - 실패하면 재시도"""
        chat_id = chat_data['id']
        
        max_retries = 3
        for attempt in range(max_retries):
            try:
                # 파이프라인으로 원자적 처리
                pipe = self.redis.pipeline()
                
                # 1. 채팅 데이터 저장 (24시간 TTL)
                chat_key = f"chat:{chat_id}"
                pipe.setex(chat_key, 86400, json.dumps(chat_data))
                
                # 2. 미답변 목록에 추가
                pipe.sadd('unanswered_chats', chat_id)
                
                # 3. 시간별 인덱스 추가 (정렬용)
                score = int(datetime.utcnow().timestamp())
                pipe.zadd('chats_by_time', {chat_id: score})
                
                # 4. 통계 업데이트
                pipe.hincrby('stats:total', 'received', 1)
                pipe.hincrby('stats:hourly', str(datetime.now().hour), 1)
                
                # 실행
                results = await pipe.execute()
                
                logger.info(f"✅ Redis 저장 성공: {chat_id} - {chat_data['customerName']}")
                
                # 현재 총 개수
                total = await self.redis.scard('unanswered_chats')
                logger.info(f"📊 현재 미답변 상담: {total}개")
                
                # WebSocket 브로드캐스트
                await self.broadcast({
                    'type': 'new_chat',
                    'chat': chat_data,
                    'total': total
                })
                
                return  # 성공
                
            except Exception as e:
                logger.error(f"⚠️ Redis 저장 실패 (시도 {attempt + 1}/{max_retries}): {e}")
                if attempt < max_retries - 1:
                    await asyncio.sleep(0.5)
                else:
                    logger.error(f"❌ 최종 실패: {chat_id}")
    
    async def remove_chat(self, chat_id: str):
        """Redis에서 제거"""
        try:
            pipe = self.redis.pipeline()
            
            # 1. 데이터 가져오기 (응답시간 계산용)
            chat_data = await self.redis.get(f"chat:{chat_id}")
            
            # 2. 삭제 처리
            pipe.delete(f"chat:{chat_id}")
            pipe.srem('unanswered_chats', chat_id)
            pipe.zrem('chats_by_time', chat_id)
            
            # 3. 통계 업데이트
            pipe.hincrby('stats:total', 'answered', 1)
            
            if chat_data:
                data = json.loads(chat_data)
                # 응답시간 계산
                created = datetime.fromisoformat(data['timestamp'].replace('Z', '+00:00')) if isinstance(data['timestamp'], str) else datetime.fromtimestamp(data['timestamp'] / 1000)
                response_time = int((datetime.utcnow() - created).total_seconds() / 60)
                
                # 응답시간 저장
                pipe.lpush('response_times', response_time)
                pipe.ltrim('response_times', 0, 999)  # 최근 1000개만
            
            results = await pipe.execute()
            
            logger.info(f"✅ Redis에서 제거: {chat_id}")
            
            # 현재 총 개수
            total = await self.redis.scard('unanswered_chats')
            logger.info(f"📊 남은 미답변 상담: {total}개")
            
            # WebSocket 브로드캐스트
            await self.broadcast({
                'type': 'chat_answered',
                'chatId': chat_id,
                'total': total
            })
            
        except Exception as e:
            logger.error(f"❌ Redis 제거 실패: {e}")
    
    async def get_all_chats(self) -> List[dict]:
        """Redis에서 모든 채팅 조회"""
        try:
            # sorted set으로 시간순 정렬된 ID 가져오기
            chat_ids = await self.redis.zrevrange('chats_by_time', 0, -1)
            
            if not chat_ids:
                # fallback: set에서 가져오기
                chat_ids = await self.redis.smembers('unanswered_chats')
            
            logger.info(f"📊 Redis에서 {len(chat_ids)}개 상담 ID 조회")
            
            chats = []
            
            if chat_ids:
                # 파이프라인으로 한번에 가져오기
                pipe = self.redis.pipeline()
                for chat_id in chat_ids:
                    pipe.get(f"chat:{chat_id}")
                
                results = await pipe.execute()
                
                for chat_json in results:
                    if chat_json:
                        try:
                            chat_data = json.loads(chat_json)
                            
                            # 대기시간 계산
                            if isinstance(chat_data['timestamp'], (int, float)):
                                created = datetime.fromtimestamp(chat_data['timestamp'] / 1000)
                            else:
                                created = datetime.fromisoformat(chat_data['timestamp'].replace('Z', '+00:00'))
                            
                            wait_minutes = int((datetime.utcnow() - created).total_seconds() / 60)
                            chat_data['waitMinutes'] = max(0, wait_minutes)
                            
                            chats.append(chat_data)
                        except Exception as e:
                            logger.error(f"채팅 파싱 오류: {e}")
            
            # 대기시간 순 정렬
            chats.sort(key=lambda x: x['waitMinutes'], reverse=True)
            
            logger.info(f"📊 최종 반환: {len(chats)}개 미답변 상담")
            return chats
            
        except Exception as e:
            logger.error(f"❌ Redis 조회 실패: {e}")
            return []
    
    async def get_stats(self) -> dict:
        """통계 조회"""
        try:
            pipe = self.redis.pipeline()
            pipe.hgetall('stats:total')
            pipe.hgetall('stats:hourly')
            pipe.lrange('response_times', 0, 99)
            pipe.scard('unanswered_chats')
            
            results = await pipe.execute()
            
            total_stats = results[0] or {}
            hourly_stats = results[1] or {}
            response_times = [int(t) for t in results[2]] if results[2] else []
            current_count = results[3] or 0
            
            # 평균 응답시간 계산
            avg_response = sum(response_times) / len(response_times) if response_times else 0
            
            return {
                'total_received': int(total_stats.get('received', 0)),
                'total_answered': int(total_stats.get('answered', 0)),
                'current_unanswered': current_count,
                'avg_response_time': round(avg_response, 1),
                'hourly': hourly_stats,
                'peak_hour': max(hourly_stats.items(), key=lambda x: int(x[1]))[0] if hourly_stats else 'N/A'
            }
            
        except Exception as e:
            logger.error(f"통계 조회 실패: {e}")
            return {}
    
    async def handle_webhook(self, request):
        """채널톡 웹훅 수신"""
        # 토큰 검증
        token = request.query.get('token')
        if token != WEBHOOK_TOKEN:
            return web.Response(status=401)
        
        try:
            data = await request.json()
            event_type = data.get('type')
            
            logger.info(f"🔔 웹훅: {event_type}")
            
            if event_type == 'message':
                await self.process_message(data)
            elif event_type == 'userChat':
                await self.process_user_chat(data)
            
            return web.json_response({"status": "ok"})
            
        except Exception as e:
            logger.error(f"웹훅 처리 오류: {e}")
            return web.Response(status=500)
    
    async def process_message(self, data: dict):
        """메시지 이벤트 처리"""
        try:
            entity = data.get('entity', {})
            refers = data.get('refers', {})
            
            chat_id = entity.get('chatId')
            person_type = entity.get('personType')
            
            if not chat_id:
                return
            
            if person_type == 'user':
                # 고객 메시지 -> 저장
                user_info = refers.get('user', {})
                user_chat = refers.get('userChat', {})
                
                chat_data = {
                    'id': str(chat_id),
                    'customerName': user_info.get('name') or user_chat.get('name', '익명'),
                    'lastMessage': entity.get('plainText', ''),
                    'timestamp': entity.get('createdAt', datetime.utcnow().isoformat()),
                    'waitMinutes': 0
                }
                
                await self.save_chat(chat_data)
                
            elif person_type in ['manager', 'bot']:
                # 답변 -> 제거
                await self.remove_chat(str(chat_id))
                
        except Exception as e:
            logger.error(f"메시지 처리 오류: {e}")
    
    async def process_user_chat(self, data: dict):
        """상담 상태 변경 처리"""
        try:
            entity = data.get('entity', {})
            chat_id = entity.get('id')
            state = entity.get('state')
            
            if state in ['closed', 'resolved'] and chat_id:
                await self.remove_chat(str(chat_id))
                
        except Exception as e:
            logger.error(f"상태 변경 처리 오류: {e}")
    
    async def get_chats(self, request):
        """API: 상담 목록"""
        chats = await self.get_all_chats()
        stats = await self.get_stats()
        
        return web.json_response({
            'chats': chats,
            'total': len(chats),
            'stats': stats,
            'timestamp': datetime.utcnow().isoformat()
        })
    
    async def mark_answered(self, request):
        """API: 수동 답변 완료"""
        chat_id = request.match_info['chat_id']
        await self.remove_chat(chat_id)
        return web.json_response({'status': 'ok'})
    
    async def health_check(self, request):
        """헬스 체크"""
        try:
            # Redis PING
            await self.redis.ping()
            redis_status = 'healthy'
            
            # Redis 정보
            info = await self.redis.info()
            memory_used = info.get('used_memory_human', 'N/A')
            
        except:
            redis_status = 'unhealthy'
            memory_used = 'N/A'
        
        stats = await self.get_stats()
        
        return web.json_response({
            'status': 'healthy',
            'redis': redis_status,
            'redis_memory': memory_used,
            'stats': stats,
            'websocket_connections': len(self.websockets),
            'timestamp': datetime.utcnow().isoformat()
        })
    
    async def handle_websocket(self, request):
        """WebSocket 연결"""
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        
        self.websockets.add(ws)
        logger.info(f"🔌 WebSocket 연결 (총 {len(self.websockets)}개)")
        
        try:
            # 초기 데이터
            chats = await self.get_all_chats()
            stats = await self.get_stats()
            
            await ws.send_json({
                'type': 'initial',
                'chats': chats,
                'stats': stats
            })
            
            # 연결 유지
            async for msg in ws:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    data = json.loads(msg.data)
                    if data.get('type') == 'ping':
                        await ws.send_json({'type': 'pong'})
                        
        except Exception as e:
            logger.error(f"WebSocket 오류: {e}")
        finally:
            self.websockets.discard(ws)
        
        return ws
    
    async def broadcast(self, data: dict):
        """WebSocket 브로드캐스트"""
        if not self.websockets:
            return
            
        dead = []
        for ws in self.websockets:
            try:
                await ws.send_json(data)
            except:
                dead.append(ws)
        
        for ws in dead:
            self.websockets.discard(ws)
    
    async def serve_dashboard(self, request):
        """대시보드"""
        return web.Response(text=DASHBOARD_HTML, content_type='text/html')

# ===== 대시보드 HTML =====
DASHBOARD_HTML = """
<!DOCTYPE html>
<html lang="ko">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>아정당 채널톡 - Redis Powered</title>
    <style>
        :root {
            --bg-primary: #0a0a0a;
            --bg-secondary: #1a1a1a;
            --bg-card: #242424;
            --text-primary: #ffffff;
            --text-secondary: #a0a0a0;
            --redis-red: #DC382D;
            --channeltalk-blue: #2563EB;
            --status-critical: #DC2626;
            --status-warning: #EA580C;
            --status-caution: #FACC15;
            --status-normal: #2563EB;
            --status-new: #10B981;
        }

        * {
            margin: 0;
            padding: 0;
            box-sizing: border-box;
        }

        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            background: linear-gradient(135deg, var(--bg-primary) 0%, #1a0000 100%);
            color: var(--text-primary);
            min-height: 100vh;
            padding: 20px;
        }

        .container {
            max-width: 1400px;
            margin: 0 auto;
        }

        .header {
            background: var(--bg-secondary);
            border-radius: 16px;
            padding: 30px;
            margin-bottom: 24px;
            border: 1px solid #333;
            position: relative;
            overflow: hidden;
        }

        .header::before {
            content: '';
            position: absolute;
            top: 0;
            left: 0;
            right: 0;
            height: 3px;
            background: linear-gradient(90deg, var(--redis-red), var(--channeltalk-blue));
            animation: gradient 3s ease infinite;
        }

        @keyframes gradient {
            0%, 100% { transform: translateX(-100%); }
            50% { transform: translateX(100%); }
        }

        .title {
            font-size: 28px;
            font-weight: 700;
            background: linear-gradient(90deg, var(--redis-red), var(--channeltalk-blue));
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
            margin-bottom: 10px;
        }

        .redis-badge {
            display: inline-block;
            background: var(--redis-red);
            color: white;
            padding: 4px 12px;
            border-radius: 4px;
            font-size: 12px;
            font-weight: 600;
            margin-bottom: 20px;
        }

        .stats {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(140px, 1fr));
            gap: 16px;
            margin-bottom: 20px;
        }

        .stat-card {
            background: var(--bg-card);
            padding: 16px;
            border-radius: 8px;
            text-align: center;
            border: 1px solid #333;
            transition: all 0.3s;
        }

        .stat-card:hover {
            transform: translateY(-2px);
            box-shadow: 0 4px 12px rgba(220, 56, 45, 0.2);
        }

        .stat-value {
            font-size: 32px;
            font-weight: 700;
        }

        .stat-label {
            font-size: 12px;
            color: var(--text-secondary);
            margin-top: 4px;
        }

        .controls {
            display: flex;
            gap: 12px;
            margin-bottom: 24px;
        }

        .btn {
            padding: 10px 20px;
            background: linear-gradient(135deg, var(--redis-red), var(--channeltalk-blue));
            border: none;
            color: white;
            border-radius: 8px;
            cursor: pointer;
            font-size: 14px;
            font-weight: 600;
            transition: all 0.3s;
        }

        .btn:hover {
            transform: translateY(-2px);
            box-shadow: 0 4px 12px rgba(220, 56, 45, 0.4);
        }

        .chat-grid {
            display: grid;
            grid-template-columns: repeat(auto-fill, minmax(380px, 1fr));
            gap: 16px;
        }

        .chat-card {
            background: var(--bg-card);
            border: 1px solid #333;
            border-radius: 12px;
            padding: 20px;
            position: relative;
            transition: all 0.3s;
            animation: slideIn 0.4s ease-out;
        }

        @keyframes slideIn {
            from {
                opacity: 0;
                transform: translateY(20px);
            }
            to {
                opacity: 1;
                transform: translateY(0);
            }
        }

        .chat-card:hover {
            transform: translateY(-4px);
            box-shadow: 0 8px 24px rgba(0,0,0,0.4);
        }

        .chat-card::before {
            content: '';
            position: absolute;
            left: 0;
            top: 0;
            bottom: 0;
            width: 4px;
            border-radius: 12px 0 0 12px;
        }

        .chat-card.critical::before { background: var(--status-critical); }
        .chat-card.warning::before { background: var(--status-warning); }
        .chat-card.caution::before { background: var(--status-caution); }
        .chat-card.normal::before { background: var(--status-normal); }
        .chat-card.new::before { background: var(--status-new); }

        .customer-name {
            font-size: 18px;
            font-weight: 600;
            margin-bottom: 8px;
        }

        .wait-badge {
            display: inline-block;
            padding: 4px 12px;
            border-radius: 6px;
            font-size: 12px;
            font-weight: 600;
            margin-bottom: 12px;
        }

        .badge-critical { background: var(--status-critical); }
        .badge-warning { background: var(--status-warning); }
        .badge-caution { background: var(--status-caution); }
        .badge-normal { background: var(--status-normal); }
        .badge-new { background: var(--status-new); }

        .message-preview {
            color: var(--text-secondary);
            font-size: 14px;
            margin-bottom: 12px;
            max-height: 40px;
            overflow: hidden;
        }

        .chat-footer {
            display: flex;
            justify-content: space-between;
            align-items: center;
            padding-top: 12px;
            border-top: 1px solid #333;
        }

        .chat-time {
            font-size: 12px;
            color: var(--text-secondary);
        }

        .empty-state {
            text-align: center;
            padding: 80px 20px;
            color: var(--text-secondary);
        }

        .connection-status {
            position: fixed;
            top: 20px;
            right: 20px;
            padding: 8px 16px;
            background: var(--bg-card);
            border-radius: 20px;
            font-size: 12px;
            display: flex;
            align-items: center;
            gap: 8px;
            border: 1px solid var(--redis-red);
        }

        .status-dot {
            width: 8px;
            height: 8px;
            border-radius: 50%;
            background: var(--redis-red);
            animation: pulse 2s infinite;
        }

        @keyframes pulse {
            0%, 100% { opacity: 1; }
            50% { opacity: 0.5; }
        }

        .debug-panel {
            background: var(--bg-secondary);
            border: 1px solid var(--redis-red);
            border-radius: 8px;
            padding: 15px;
            margin-bottom: 20px;
            font-family: monospace;
            font-size: 12px;
            color: var(--text-secondary);
        }
    </style>
</head>
<body>
    <div class="connection-status">
        <span class="status-dot"></span>
        <span id="connectionText">Redis Connected</span>
    </div>

    <div class="container">
        <div class="header">
            <h1 class="title">🔷 채널톡 미답변 상담 모니터</h1>
            <div class="redis-badge">⚡ Redis Standard 1GB Powered</div>
            <div class="stats">
                <div class="stat-card">
                    <div class="stat-value" id="totalCount">0</div>
                    <div class="stat-label">전체</div>
                </div>
                <div class="stat-card">
                    <div class="stat-value" style="color: var(--status-critical)" id="criticalCount">0</div>
                    <div class="stat-label">11분↑</div>
                </div>
                <div class="stat-card">
                    <div class="stat-value" style="color: var(--status-warning)" id="warningCount">0</div>
                    <div class="stat-label">8-10분</div>
                </div>
                <div class="stat-card">
                    <div class="stat-value" style="color: var(--status-caution)" id="cautionCount">0</div>
                    <div class="stat-label">5-7분</div>
                </div>
                <div class="stat-card">
                    <div class="stat-value" style="color: var(--status-normal)" id="normalCount">0</div>
                    <div class="stat-label">2-4분</div>
                </div>
                <div class="stat-card">
                    <div class="stat-value" style="color: var(--status-new)" id="newCount">0</div>
                    <div class="stat-label">신규</div>
                </div>
            </div>
        </div>

        <div class="controls">
            <button class="btn" onclick="refreshData()">🔄 새로고침</button>
            <button class="btn" onclick="showDebug()">📊 통계</button>
        </div>

        <div class="debug-panel" id="debugPanel" style="display: none;">
            <div id="debugInfo">통계 로딩 중...</div>
        </div>

        <div class="chat-grid" id="chatGrid">
            <!-- 채팅 카드 동적 생성 -->
        </div>
    </div>

    <script>
        let ws = null;
        let chats = [];
        let stats = {};

        function getPriority(minutes) {
            if (minutes >= 11) return 'critical';
            if (minutes >= 8) return 'warning';
            if (minutes >= 5) return 'caution';
            if (minutes >= 2) return 'normal';
            return 'new';
        }

        function formatWaitTime(minutes) {
            if (minutes < 1) return '방금';
            if (minutes < 60) return `${Math.floor(minutes)}분`;
            const hours = Math.floor(minutes / 60);
            const mins = minutes % 60;
            return `${hours}시간 ${mins}분`;
        }

        function renderChats() {
            const grid = document.getElementById('chatGrid');
            
            if (chats.length === 0) {
                grid.innerHTML = `
                    <div class="empty-state">
                        <div style="font-size: 64px; margin-bottom: 20px;">✨</div>
                        <h2>모든 상담이 처리되었습니다</h2>
                        <p>Redis에 저장된 미답변 상담이 없습니다</p>
                    </div>
                `;
            } else {
                grid.innerHTML = chats.map(chat => {
                    const priority = getPriority(chat.waitMinutes);
                    return `
                        <div class="chat-card ${priority}">
                            <div class="customer-name">${chat.customerName || '익명'}</div>
                            <div class="wait-badge badge-${priority}">⏱️ ${formatWaitTime(chat.waitMinutes)}</div>
                            <div class="message-preview">${chat.lastMessage || '(메시지 없음)'}</div>
                            <div class="chat-footer">
                                <span class="chat-time">${new Date(chat.timestamp).toLocaleTimeString('ko-KR')}</span>
                                <button class="btn" onclick="markAnswered('${chat.id}')">완료</button>
                            </div>
                        </div>
                    `;
                }).join('');
            }
            
            updateStats();
        }

        function updateStats() {
            document.getElementById('totalCount').textContent = chats.length;
            document.getElementById('criticalCount').textContent = chats.filter(c => c.waitMinutes >= 11).length;
            document.getElementById('warningCount').textContent = chats.filter(c => c.waitMinutes >= 8 && c.waitMinutes < 11).length;
            document.getElementById('cautionCount').textContent = chats.filter(c => c.waitMinutes >= 5 && c.waitMinutes < 8).length;
            document.getElementById('normalCount').textContent = chats.filter(c => c.waitMinutes >= 2 && c.waitMinutes < 5).length;
            document.getElementById('newCount').textContent = chats.filter(c => c.waitMinutes < 2).length;
        }

        function connectWebSocket() {
            const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
            const wsUrl = `${protocol}//${window.location.host}/ws`;
            
            ws = new WebSocket(wsUrl);
            
            ws.onopen = () => {
                console.log('✅ WebSocket 연결됨');
                document.getElementById('connectionText').textContent = 'Redis + WebSocket Live';
            };
            
            ws.onmessage = (event) => {
                const data = JSON.parse(event.data);
                console.log('WebSocket 메시지:', data);
                
                if (data.type === 'initial') {
                    chats = data.chats;
                    stats = data.stats;
                    renderChats();
                } else if (data.type === 'new_chat') {
                    chats.push(data.chat);
                    chats.sort((a, b) => b.waitMinutes - a.waitMinutes);
                    renderChats();
                } else if (data.type === 'chat_answered') {
                    chats = chats.filter(c => c.id !== data.chatId);
                    renderChats();
                }
            };
            
            ws.onerror = (error) => {
                console.error('❌ WebSocket 오류:', error);
                document.getElementById('connectionText').textContent = 'Redis Only';
                fetchData();
            };
            
            ws.onclose = () => {
                document.getElementById('connectionText').textContent = '재연결 중...';
                setTimeout(connectWebSocket, 5000);
            };
        }

        async function fetchData() {
            try {
                const response = await fetch('/api/chats');
                const data = await response.json();
                chats = data.chats;
                stats = data.stats;
                renderChats();
            } catch (error) {
                console.error('데이터 로드 실패:', error);
            }
        }

        function refreshData() {
            fetchData();
        }

        async function markAnswered(chatId) {
            await fetch(`/api/chats/${chatId}/answer`, { method: 'POST' });
            chats = chats.filter(c => c.id !== chatId);
            renderChats();
        }

        async function showDebug() {
            const panel = document.getElementById('debugPanel');
            const info = document.getElementById('debugInfo');
            
            if (panel.style.display === 'none') {
                panel.style.display = 'block';
                
                try {
                    const response = await fetch('/health');
                    const data = await response.json();
                    
                    info.innerHTML = `
                        <strong>🔴 Redis 상태:</strong> ${data.redis}<br>
                        <strong>💾 Redis 메모리:</strong> ${data.redis_memory}<br>
                        <strong>📊 통계:</strong><br>
                        - 총 접수: ${data.stats.total_received || 0}<br>
                        - 총 답변: ${data.stats.total_answered || 0}<br>
                        - 현재 미답변: ${data.stats.current_unanswered || 0}<br>
                        - 평균 응답시간: ${data.stats.avg_response_time || 0}분<br>
                        - 피크 시간대: ${data.stats.peak_hour || 'N/A'}시<br>
                        <strong>🔌 WebSocket:</strong> ${data.websocket_connections}개 연결<br>
                    `;
                } catch (error) {
                    info.textContent = '통계를 가져올 수 없습니다: ' + error;
                }
            } else {
                panel.style.display = 'none';
            }
        }

        // 초기화
        console.log('🚀 Redis 기반 모니터링 시작');
        connectWebSocket();
        fetchData();
        setInterval(fetchData, 10000); // 10초마다 동기화
    </script>
</body>
</html>
"""

# ===== 앱 생성 =====
async def create_app():
    """애플리케이션 생성"""
    logger.info("🏗️ Redis 중심 애플리케이션 생성")
    
    monitor = ChannelTalkMonitor()
    await monitor.setup()
    
    app = web.Application()
    app['monitor'] = monitor
    
    # 라우트 설정
    app.router.add_post('/webhook', monitor.handle_webhook)
    app.router.add_get('/api/chats', monitor.get_chats)
    app.router.add_post('/api/chats/{chat_id}/answer', monitor.mark_answered)
    app.router.add_get('/ws', monitor.handle_websocket)
    app.router.add_get('/health', monitor.health_check)
    app.router.add_get('/', monitor.serve_dashboard)
    
    # CORS
    async def cors_middleware(app, handler):
        async def middleware_handler(request):
            if request.method == 'OPTIONS':
                return web.Response(headers={
                    'Access-Control-Allow-Origin': '*',
                    'Access-Control-Allow-Methods': 'GET, POST, OPTIONS',
                    'Access-Control-Allow-Headers': 'Content-Type'
                })
            
            response = await handler(request)
            response.headers['Access-Control-Allow-Origin'] = '*'
            return response
        return middleware_handler
    
    app.middlewares.append(cors_middleware)
    
    # 시작/종료
    async def on_startup(app):
        logger.info("=" * 50)
        logger.info("🔴 Redis Standard 1GB 모니터링 시스템")
        logger.info(f"📌 대시보드: http://localhost:{PORT}")
        logger.info(f"💰 돈값하는 Redis 활용 중!")
        logger.info("=" * 50)
    
    async def on_cleanup(app):
        await monitor.cleanup()
        logger.info("👋 서버 종료")
    
    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    
    return app

# ===== 메인 실행 =====
if __name__ == '__main__':
    logger.info("🏁 Redis 최적화 프로그램 시작")
    loop = asyncio.get_event_loop()
    app = loop.run_until_complete(create_app())
    web.run_app(app, host='0.0.0.0', port=PORT)
