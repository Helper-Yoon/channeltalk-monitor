import asyncio
import aiohttp
from aiohttp import web
import aioredis
import json
import os
from datetime import datetime, timedelta
import logging
from typing import Dict, List, Optional, Set
import weakref

# 환경 변수
REDIS_URL = os.getenv('REDIS_URL', 'redis://localhost:6379')
CHANNEL_API_KEY = os.getenv('CHANNEL_API_KEY')
CHANNEL_API_SECRET = os.getenv('CHANNEL_API_SECRET')
WEBHOOK_TOKEN = os.getenv('WEBHOOK_TOKEN', '80ab2d11835f44b89010c8efa5eec4b4')
PORT = int(os.getenv('PORT', 10000))

# 로깅 설정
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

class ChannelTalkMonitor:
    def __init__(self):
        self.redis: Optional[aioredis.Redis] = None
        self.websockets: Set[weakref.ref] = set()
        self.webhook_token = WEBHOOK_TOKEN
        self.channel_api_key = CHANNEL_API_KEY
        self.channel_api_secret = CHANNEL_API_SECRET
        # 메모리 캐시 (Redis 실패 시 백업)
        self.memory_cache = {}
        
    async def setup(self):
        """Redis 연결 초기화"""
        try:
            self.redis = await aioredis.create_redis_pool(
                REDIS_URL,
                encoding='utf-8',
                minsize=5,
                maxsize=20
            )
            logger.info("✅ Redis 연결 성공")
        except Exception as e:
            logger.error(f"❌ Redis 연결 실패: {e}")
            self.redis = None
            logger.info("📝 메모리 캐시 모드로 실행")
    
    async def cleanup(self):
        """리소스 정리"""
        if self.redis:
            self.redis.close()
            await self.redis.wait_closed()
    
    def calculate_wait_time(self, timestamp: str) -> int:
        """대기 시간 계산 (분 단위)"""
        try:
            if isinstance(timestamp, str):
                # ISO format 처리
                created_time = datetime.fromisoformat(timestamp.replace('Z', '+00:00'))
            else:
                created_time = datetime.fromtimestamp(timestamp)
            
            wait_time = (datetime.utcnow() - created_time).total_seconds() / 60
            return max(0, int(wait_time))
        except Exception as e:
            logger.error(f"시간 계산 오류: {e}")
            return 0
    
    async def save_chat(self, chat_data: dict):
        """Redis 또는 메모리에 채팅 저장"""
        chat_id = chat_data['id']
        
        if self.redis:
            try:
                # Redis 저장
                key = f"chat:{chat_id}"
                await self.redis.setex(key, 3600, json.dumps(chat_data))
                await self.redis.sadd('unanswered_chats', chat_id)
                logger.info(f"💾 Redis 저장: {chat_id}")
            except Exception as e:
                logger.error(f"Redis 저장 실패: {e}")
                self.memory_cache[chat_id] = chat_data
        else:
            # 메모리 캐시 저장
            self.memory_cache[chat_id] = chat_data
            logger.info(f"💾 메모리 저장: {chat_id}")
    
    async def remove_chat(self, chat_id: str):
        """채팅 제거"""
        if self.redis:
            try:
                await self.redis.delete(f"chat:{chat_id}")
                await self.redis.srem('unanswered_chats', chat_id)
                logger.info(f"🗑️ Redis 삭제: {chat_id}")
            except Exception as e:
                logger.error(f"Redis 삭제 실패: {e}")
                self.memory_cache.pop(chat_id, None)
        else:
            self.memory_cache.pop(chat_id, None)
            logger.info(f"🗑️ 메모리 삭제: {chat_id}")
    
    async def get_all_chats(self) -> List[dict]:
        """모든 미답변 채팅 조회"""
        chats = []
        
        if self.redis:
            try:
                # Redis에서 조회
                chat_ids = await self.redis.smembers('unanswered_chats')
                logger.info(f"📊 Redis에서 {len(chat_ids)}개 채팅 발견")
                
                for chat_id in chat_ids:
                    key = f"chat:{chat_id}"
                    chat_json = await self.redis.get(key)
                    if chat_json:
                        chat_data = json.loads(chat_json)
                        chat_data['waitMinutes'] = self.calculate_wait_time(chat_data['timestamp'])
                        chats.append(chat_data)
            except Exception as e:
                logger.error(f"Redis 조회 실패: {e}")
                # 메모리 캐시로 폴백
                for chat_data in self.memory_cache.values():
                    chat_data['waitMinutes'] = self.calculate_wait_time(chat_data['timestamp'])
                    chats.append(chat_data)
        else:
            # 메모리 캐시에서 조회
            for chat_data in self.memory_cache.values():
                chat_data['waitMinutes'] = self.calculate_wait_time(chat_data['timestamp'])
                chats.append(chat_data)
            logger.info(f"📊 메모리에서 {len(chats)}개 채팅 발견")
        
        # 대기시간 순 정렬
        chats.sort(key=lambda x: x['waitMinutes'], reverse=True)
        return chats
    
    async def handle_webhook(self, request):
        """채널톡 웹훅 처리"""
        # 토큰 검증 (대소문자 무시)
        tokens = request.query.getall('token', [])
        valid_tokens = ['80ab2d11835f44b89010c8efa5eec4b4', 'AJUNG', 'ajung', self.webhook_token]
        
        token_valid = False
        for token in tokens:
            if token.upper() in [t.upper() for t in valid_tokens]:
                token_valid = True
                break
        
        if not token_valid:
            logger.warning(f"❌ 잘못된 웹훅 토큰: {tokens}")
            return web.Response(status=401)
        
        try:
            data = await request.json()
            event_type = data.get('type')
            
            logger.info(f"📨 웹훅 수신: {event_type}")
            logger.debug(f"웹훅 데이터: {json.dumps(data, ensure_ascii=False)[:500]}")
            
            if event_type == 'message':
                await self.process_message(data)
            elif event_type == 'userChat':
                await self.process_user_chat(data)
            
            return web.json_response({"status": "ok"})
        except Exception as e:
            logger.error(f"❌ 웹훅 처리 오류: {e}", exc_info=True)
            return web.Response(status=500)
    
    async def process_message(self, data: dict):
        """메시지 이벤트 처리"""
        try:
            message = data.get('message', {})
            chat_id = message.get('chatId')
            person_type = message.get('personType')
            
            if not chat_id:
                logger.warning("chat_id가 없는 메시지")
                return
            
            logger.info(f"메시지 처리: chat_id={chat_id}, person_type={person_type}")
            
            # 고객 메시지인 경우
            if person_type == 'user' and not message.get('isBot', False):
                user_chat = data.get('userChat', {})
                
                # 고객 이름 추출 (여러 방법 시도)
                customer_name = (
                    user_chat.get('name') or 
                    user_chat.get('profile', {}).get('name') or
                    message.get('personName') or
                    '익명'
                )
                
                chat_data = {
                    'id': chat_id,
                    'customerName': customer_name,
                    'lastMessage': message.get('plainText', ''),
                    'timestamp': message.get('createdAt', datetime.utcnow().isoformat()),
                    'waitMinutes': 0
                }
                
                await self.save_chat(chat_data)
                
                # WebSocket 브로드캐스트
                await self.broadcast({
                    'type': 'new_chat',
                    'chat': chat_data
                })
                
                logger.info(f"✅ 새 상담 저장: {customer_name} - {chat_data['lastMessage'][:50]}")
            
            # 매니저/봇 답변인 경우
            elif person_type in ['manager', 'bot']:
                await self.remove_chat(chat_id)
                
                # WebSocket 브로드캐스트
                await self.broadcast({
                    'type': 'chat_answered',
                    'chatId': chat_id
                })
                
                logger.info(f"✅ 답변 완료: {chat_id}")
                
        except Exception as e:
            logger.error(f"메시지 처리 오류: {e}", exc_info=True)
    
    async def process_user_chat(self, data: dict):
        """유저챗 상태 변경 처리"""
        try:
            user_chat = data.get('userChat', {})
            chat_id = user_chat.get('id')
            state = user_chat.get('state')
            
            logger.info(f"유저챗 처리: chat_id={chat_id}, state={state}")
            
            # 상담 종료된 경우
            if state == 'closed' and chat_id:
                await self.remove_chat(chat_id)
                
                await self.broadcast({
                    'type': 'chat_answered',
                    'chatId': chat_id
                })
                
                logger.info(f"✅ 상담 종료: {chat_id}")
        except Exception as e:
            logger.error(f"유저챗 처리 오류: {e}", exc_info=True)
    
    async def get_chats(self, request):
        """미답변 상담 목록 API"""
        try:
            chats = await self.get_all_chats()
            
            response_data = {
                'chats': chats,
                'total': len(chats),
                'timestamp': datetime.utcnow().isoformat()
            }
            
            logger.info(f"📋 API 응답: {len(chats)}개 상담")
            
            return web.json_response(response_data, headers={
                'Cache-Control': 'no-cache'
            })
        except Exception as e:
            logger.error(f"API 오류: {e}", exc_info=True)
            return web.json_response({'chats': [], 'total': 0, 'error': str(e)})
    
    async def mark_answered(self, request):
        """답변 완료 처리"""
        chat_id = request.match_info['chat_id']
        await self.remove_chat(chat_id)
        
        await self.broadcast({
            'type': 'chat_answered',
            'chatId': chat_id
        })
        
        return web.json_response({'status': 'ok'})
    
    async def handle_websocket(self, request):
        """WebSocket 연결 처리"""
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        
        # 연결 추가
        ws_ref = weakref.ref(ws)
        self.websockets.add(ws_ref)
        logger.info(f"🔌 WebSocket 연결됨. 총 {len(self.websockets)}개 연결")
        
        try:
            # 초기 데이터 전송
            chats = await self.get_all_chats()
            await ws.send_json({
                'type': 'bulk_update',
                'chats': chats
            })
            
            # 연결 유지
            async for msg in ws:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    data = json.loads(msg.data)
                    if data.get('type') == 'ping':
                        await ws.send_json({'type': 'pong'})
                elif msg.type == aiohttp.WSMsgType.ERROR:
                    logger.error(f'WebSocket 오류: {ws.exception()}')
        finally:
            self.websockets.discard(ws_ref)
            logger.info(f"🔌 WebSocket 연결 해제. 남은 연결: {len(self.websockets)}개")
        
        return ws
    
    async def broadcast(self, data: dict):
        """모든 WebSocket 클라이언트에 브로드캐스트"""
        dead_refs = set()
        
        for ws_ref in self.websockets:
            ws = ws_ref()
            if ws is None:
                dead_refs.add(ws_ref)
            else:
                try:
                    await ws.send_json(data)
                except Exception as e:
                    logger.error(f"브로드캐스트 실패: {e}")
                    dead_refs.add(ws_ref)
        
        self.websockets -= dead_refs
    
    async def health_check(self, request):
        """헬스 체크"""
        chats = await self.get_all_chats()
        
        return web.json_response({
            'status': 'healthy',
            'redis': 'connected' if self.redis else 'memory_mode',
            'unanswered_count': len(chats),
            'websocket_connections': len(self.websockets),
            'memory_cache_count': len(self.memory_cache),
            'timestamp': datetime.utcnow().isoformat()
        })
    
    async def serve_dashboard(self, request):
        """대시보드 HTML 서빙"""
        html_path = os.path.join(os.path.dirname(__file__), 'dashboard.html')
        
        if os.path.exists(html_path):
            return web.FileResponse(html_path)
        else:
            # HTML이 없을 경우 기본 대시보드 제공
            return web.Response(text=DEFAULT_DASHBOARD_HTML, content_type='text/html')

# 기본 대시보드 HTML (dashboard.html이 없을 경우)
DEFAULT_DASHBOARD_HTML = """
<!DOCTYPE html>
<html>
<head>
    <title>채널톡 미답변 상담 모니터</title>
    <meta charset="utf-8">
    <style>
        body { 
            background: #1a1a1a; 
            color: white; 
            font-family: sans-serif; 
            padding: 20px;
        }
        h1 { color: #2563EB; }
        .status { 
            background: #242424; 
            padding: 20px; 
            border-radius: 10px; 
            margin: 20px 0;
        }
    </style>
</head>
<body>
    <h1>채널톡 미답변 상담 모니터</h1>
    <div class="status">
        <p>서버가 실행 중입니다.</p>
        <p>dashboard.html 파일을 업로드하여 완전한 대시보드를 사용하세요.</p>
        <p><a href="/health" style="color: #2563EB;">헬스 체크</a></p>
        <p><a href="/api/chats" style="color: #2563EB;">상담 목록 API</a></p>
    </div>
</body>
</html>
"""

async def create_app():
    """애플리케이션 생성"""
    monitor = ChannelTalkMonitor()
    
    # Redis 초기화
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
    
    # CORS 미들웨어
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
            response.headers['Access-Control-Allow-Methods'] = 'GET, POST, OPTIONS'
            response.headers['Access-Control-Allow-Headers'] = 'Content-Type'
            return response
        return middleware_handler
    
    app.middlewares.append(cors_middleware)
    
    # 백그라운드 태스크
    async def start_background_tasks(app):
        logger.info("🚀 서버 시작됨!")
    
    async def cleanup_background_tasks(app):
        await monitor.cleanup()
        logger.info("👋 서버 종료됨")
    
    app.on_startup.append(start_background_tasks)
    app.on_cleanup.append(cleanup_background_tasks)
    
    return app

if __name__ == '__main__':
    logger.info("=" * 50)
    logger.info("채널톡 미답변 상담 모니터 시작")
    logger.info("=" * 50)
    
    loop = asyncio.get_event_loop()
    app = loop.run_until_complete(create_app())
    web.run_app(app, host='0.0.0.0', port=PORT)
