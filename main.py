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
WEBHOOK_TOKEN = '80ab2d11835f44b89010c8efa5eec4b4'  # 하드코딩 (환경변수 문제 해결)
PORT = int(os.getenv('PORT', 10000))

# ===== 로깅 설정 =====
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

class ChannelTalkMonitor:
    """Redis 기반 채널톡 모니터링"""
    
    def __init__(self):
        self.redis: Optional[aioredis.Redis] = None
        self.websockets = weakref.WeakSet()
        logger.info("🚀 ChannelTalkMonitor 초기화")
        
    async def setup(self):
        """Redis 연결"""
        try:
            # 동일한 이벤트 루프에서 Redis 생성
            self.redis = await aioredis.from_url(
                REDIS_URL,
                decode_responses=True,
                health_check_interval=30,
                socket_connect_timeout=5,
                retry_on_timeout=True,
                socket_keepalive=True
            )
            
            # 연결 테스트
            await self.redis.ping()
            logger.info("✅ Redis 연결 성공!")
            
            # 기존 데이터 확인
            existing_count = await self.redis.scard('unanswered_chats')
            logger.info(f"📥 기존 미답변 상담: {existing_count}개")
            
        except Exception as e:
            logger.error(f"❌ Redis 연결 실패: {e}")
            raise
    
    async def cleanup(self):
        """종료시 정리"""
        if self.redis:
            await self.redis.close()
            logger.info("👋 Redis 연결 종료")
    
    async def save_chat(self, chat_data: dict):
        """Redis에 저장"""
        chat_id = chat_data['id']
        
        try:
            # 파이프라인으로 원자적 처리
            async with self.redis.pipeline() as pipe:
                # 채팅 데이터 저장
                chat_key = f"chat:{chat_id}"
                await pipe.setex(chat_key, 86400, json.dumps(chat_data))
                
                # 미답변 목록에 추가
                await pipe.sadd('unanswered_chats', chat_id)
                
                # 시간별 인덱스
                score = int(datetime.utcnow().timestamp())
                await pipe.zadd('chats_by_time', {chat_id: score})
                
                # 통계
                await pipe.hincrby('stats:total', 'received', 1)
                
                await pipe.execute()
            
            logger.info(f"✅ 저장: {chat_id} - {chat_data['customerName']}")
            
            # 현재 총 개수
            total = await self.redis.scard('unanswered_chats')
            logger.info(f"📊 현재 미답변: {total}개")
            
            # WebSocket 브로드캐스트
            await self.broadcast({
                'type': 'new_chat',
                'chat': chat_data,
                'total': total
            })
            
        except Exception as e:
            logger.error(f"❌ Redis 저장 실패: {e}")
    
    async def remove_chat(self, chat_id: str):
        """Redis에서 제거"""
        try:
            async with self.redis.pipeline() as pipe:
                await pipe.delete(f"chat:{chat_id}")
                await pipe.srem('unanswered_chats', chat_id)
                await pipe.zrem('chats_by_time', chat_id)
                await pipe.hincrby('stats:total', 'answered', 1)
                await pipe.execute()
            
            logger.info(f"✅ 제거: {chat_id}")
            
            total = await self.redis.scard('unanswered_chats')
            
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
            if not self.redis:
                logger.warning("Redis 연결 없음")
                return []
                
            # Sorted Set으로 시간순 정렬된 ID 가져오기
            chat_ids = await self.redis.zrevrange('chats_by_time', 0, -1)
            
            if not chat_ids:
                # Fallback: Set에서 가져오기
                chat_ids = await self.redis.smembers('unanswered_chats')
            
            logger.info(f"📊 Redis에서 {len(chat_ids)}개 상담 ID 조회")
            
            chats = []
            
            if chat_ids:
                # 파이프라인으로 한번에 가져오기
                async with self.redis.pipeline() as pipe:
                    for chat_id in chat_ids:
                        await pipe.get(f"chat:{chat_id}")
                    
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
    
    async def handle_webhook(self, request):
        """채널톡 웹훅 수신"""
        # 토큰 검증 - 간단하게
        tokens = request.query.getall('token', [])
        
        # 토큰 확인 - 하나라도 일치하면 OK
        if '80ab2d11835f44b89010c8efa5eec4b4' not in tokens:
            logger.warning(f"❌ 잘못된 토큰: {tokens}")
            return web.Response(status=401)
        
        logger.info(f"✅ 웹훅 토큰 확인 완료")
        
        try:
            data = await request.json()
            event_type = data.get('type')
            
            logger.info(f"✅ 웹훅 수신: {event_type}")
            
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
            
            logger.info(f"📨 메시지: chat_id={chat_id}, person_type={person_type}")
            
            if person_type == 'user':
                # 고객 메시지 저장
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
                # 답변시 제거
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
                logger.info(f"💬 상담 종료: {chat_id}")
                await self.remove_chat(str(chat_id))
                
        except Exception as e:
            logger.error(f"상태 변경 처리 오류: {e}")
    
    async def get_chats(self, request):
        """API: 상담 목록"""
        chats = await self.get_all_chats()
        
        # 통계
        try:
            stats = await self.redis.hgetall('stats:total') if self.redis else {}
        except:
            stats = {}
        
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
            await self.redis.ping()
            redis_status = 'healthy'
        except:
            redis_status = 'unhealthy'
        
        return web.json_response({
            'status': 'healthy',
            'redis': redis_status,
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
            
            await ws.send_json({
                'type': 'initial',
                'chats': chats
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
        """대시보드 HTML"""
        return web.Response(text=DASHBOARD_HTML, content_type='text/html')

# ===== 대시보드 HTML (동일) =====
DASHBOARD_HTML = """
<!DOCTYPE html>
<html lang="ko">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>아정당 채널톡 모니터</title>
    <style>
        :root {
            --bg-primary: #0a0a0a;
            --bg-secondary: #1a1a1a;
            --bg-card: #242424;
            --text-primary: #ffffff;
            --text-secondary: #a0a0a0;
            --channeltalk-blue: #2563EB;
            --critical: #DC2626;
            --warning: #EA580C;
            --caution: #FACC15;
            --normal: #2563EB;
            --new: #10B981;
        }

        * { margin: 0; padding: 0; box-sizing: border-box; }

        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
            background: var(--bg-primary);
            color: var(--text-primary);
            min-height: 100vh;
            padding: 20px;
        }

        .container { max-width: 1400px; margin: 0 auto; }

        .header {
            background: var(--bg-secondary);
            border-radius: 16px;
            padding: 30px;
            margin-bottom: 24px;
            border: 1px solid #333;
        }

        .title {
            font-size: 28px;
            font-weight: 700;
            color: var(--channeltalk-blue);
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
            from { opacity: 0; transform: translateY(20px); }
            to { opacity: 1; transform: translateY(0); }
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

        .chat-card.critical::before { background: var(--critical); }
        .chat-card.warning::before { background: var(--warning); }
        .chat-card.caution::before { background: var(--caution); }
        .chat-card.normal::before { background: var(--normal); }
        .chat-card.new::before { background: var(--new); }

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

        .badge-critical { background: var(--critical); }
        .badge-warning { background: var(--warning); }
        .badge-caution { background: var(--caution); }
        .badge-normal { background: var(--normal); }
        .badge-new { background: var(--new); }

        .message-preview {
            color: var(--text-secondary);
            font-size: 14px;
            margin-bottom: 12px;
            max-height: 40px;
            overflow: hidden;
        }

        .empty-state {
            text-align: center;
            padding: 80px 20px;
            color: var(--text-secondary);
        }

        .btn {
            padding: 10px 20px;
            background: var(--bg-card);
            border: 1px solid #333;
            color: white;
            border-radius: 8px;
            cursor: pointer;
            font-size: 14px;
            font-weight: 600;
            transition: all 0.3s;
        }

        .btn:hover {
            background: var(--channeltalk-blue);
            transform: translateY(-2px);
        }
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <h1 class="title">🔷 채널톡 미답변 상담 모니터</h1>
            <div class="stats">
                <div class="stat-card">
                    <div class="stat-value" id="totalCount">0</div>
                    <div class="stat-label">전체</div>
                </div>
                <div class="stat-card">
                    <div class="stat-value" style="color: var(--critical)" id="criticalCount">0</div>
                    <div class="stat-label">11분↑</div>
                </div>
                <div class="stat-card">
                    <div class="stat-value" style="color: var(--warning)" id="warningCount">0</div>
                    <div class="stat-label">8-10분</div>
                </div>
                <div class="stat-card">
                    <div class="stat-value" style="color: var(--caution)" id="cautionCount">0</div>
                    <div class="stat-label">5-7분</div>
                </div>
                <div class="stat-card">
                    <div class="stat-value" style="color: var(--normal)" id="normalCount">0</div>
                    <div class="stat-label">2-4분</div>
                </div>
                <div class="stat-card">
                    <div class="stat-value" style="color: var(--new)" id="newCount">0</div>
                    <div class="stat-label">신규</div>
                </div>
            </div>
        </div>

        <div class="chat-grid" id="chatGrid">
            <!-- 채팅 카드 동적 생성 -->
        </div>
    </div>

    <script>
        let ws = null;
        let chats = [];

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
                        <p>현재 대기 중인 미답변 상담이 없습니다</p>
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
                            <button class="btn" onclick="markAnswered('${chat.id}')">완료</button>
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
            };
            
            ws.onmessage = (event) => {
                const data = JSON.parse(event.data);
                
                if (data.type === 'initial') {
                    chats = data.chats;
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
                console.error('WebSocket 오류:', error);
                fetchData();
            };
            
            ws.onclose = () => {
                setTimeout(connectWebSocket, 5000);
            };
        }

        async function fetchData() {
            try {
                const response = await fetch('/api/chats');
                const data = await response.json();
                chats = data.chats;
                renderChats();
            } catch (error) {
                console.error('데이터 로드 실패:', error);
            }
        }

        async function markAnswered(chatId) {
            await fetch(`/api/chats/${chatId}/answer`, { method: 'POST' });
            chats = chats.filter(c => c.id !== chatId);
            renderChats();
        }

        // 초기화
        connectWebSocket();
        fetchData();
        setInterval(fetchData, 10000);
    </script>
</body>
</html>
"""

# ===== 앱 생성 =====
async def create_app():
    """애플리케이션 생성"""
    logger.info("🏗️ 애플리케이션 생성 시작")
    
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
        logger.info("🚀 채널톡 모니터링 시스템 시작")
        logger.info(f"📌 대시보드: http://localhost:{PORT}")
        logger.info("=" * 50)
    
    async def on_cleanup(app):
        await monitor.cleanup()
        logger.info("👋 서버 종료")
    
    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    
    return app

# ===== 메인 실행 =====
if __name__ == '__main__':
    logger.info("🏁 프로그램 시작")
    
    async def main():
        app = await create_app()
        return app
    
    app = asyncio.run(main())
    web.run_app(app, host='0.0.0.0', port=PORT)
