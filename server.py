import asyncio
import aiohttp
from aiohttp import web
import aioredis
import json
import os
from datetime import datetime, timedelta
import logging
from typing import Dict, List, Optional, Set
import hashlib
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
        
    async def setup(self):
        """Redis 연결 초기화"""
        try:
            self.redis = await aioredis.create_redis_pool(
                REDIS_URL,
                encoding='utf-8',
                minsize=5,
                maxsize=20
            )
            logger.info("Redis 연결 성공")
            
            # 초기 데이터 로드
            await self.initial_sync()
        except Exception as e:
            logger.error(f"Redis 연결 실패: {e}")
            # Redis 실패시 메모리 캐시로 폴백
            self.redis = None
            self.memory_cache = {}
    
    async def cleanup(self):
        """리소스 정리"""
        if self.redis:
            self.redis.close()
            await self.redis.wait_closed()
    
    def calculate_wait_time(self, timestamp: str) -> int:
        """대기 시간 계산 (분 단위)"""
        try:
            if isinstance(timestamp, str):
                created_time = datetime.fromisoformat(timestamp.replace('Z', '+00:00'))
            else:
                created_time = datetime.fromtimestamp(timestamp)
            wait_time = (datetime.utcnow() - created_time).total_seconds() / 60
            return max(0, int(wait_time))
        except Exception as e:
            logger.error(f"시간 계산 오류: {e}")
            return 0
    
    async def get_chat_key(self, chat_id: str) -> str:
        """Redis 키 생성"""
        return f"chat:{chat_id}"
    
    async def save_chat(self, chat_data: dict):
        """Redis에 채팅 저장"""
        chat_id = chat_data['id']
        key = await self.get_chat_key(chat_id)
        
        if self.redis:
            # Redis 저장
            await self.redis.setex(
                key,
                3600,  # 1시간 TTL
                json.dumps(chat_data)
            )
            # 인덱스에 추가
            await self.redis.sadd('unanswered_chats', chat_id)
        else:
            # 메모리 캐시 폴백
            self.memory_cache[chat_id] = chat_data
    
    async def remove_chat(self, chat_id: str):
        """채팅 제거"""
        key = await self.get_chat_key(chat_id)
        
        if self.redis:
            await self.redis.delete(key)
            await self.redis.srem('unanswered_chats', chat_id)
        else:
            self.memory_cache.pop(chat_id, None)
    
    async def get_all_chats(self) -> List[dict]:
        """모든 미답변 채팅 조회"""
        chats = []
        
        if self.redis:
            # Redis에서 조회
            chat_ids = await self.redis.smembers('unanswered_chats')
            
            if chat_ids:
                # 파이프라인으로 효율적 조회
                pipe = self.redis.pipeline()
                for chat_id in chat_ids:
                    key = await self.get_chat_key(chat_id)
                    pipe.get(key)
                
                results = await pipe.execute()
                
                for result in results:
                    if result:
                        chat_data = json.loads(result)
                        # 대기시간 실시간 계산
                        chat_data['waitMinutes'] = self.calculate_wait_time(chat_data['timestamp'])
                        chats.append(chat_data)
        else:
            # 메모리 캐시에서 조회
            for chat_data in self.memory_cache.values():
                chat_data['waitMinutes'] = self.calculate_wait_time(chat_data['timestamp'])
                chats.append(chat_data)
        
        # 대기시간 순 정렬
        chats.sort(key=lambda x: x['waitMinutes'], reverse=True)
        return chats
    
    async def handle_webhook(self, request):
        """채널톡 웹훅 처리"""
        # 토큰 검증
        token = request.query.get('token')
        if token != self.webhook_token:
            logger.warning(f"잘못된 웹훅 토큰: {token}")
            return web.Response(status=401)
        
        try:
            data = await request.json()
            event_type = data.get('type')
            
            logger.info(f"웹훅 수신: {event_type}")
            
            if event_type == 'message':
                await self.process_message(data)
            elif event_type == 'userChat':
                await self.process_user_chat(data)
            
            return web.json_response({"status": "ok"})
        except Exception as e:
            logger.error(f"웹훅 처리 오류: {e}", exc_info=True)
            return web.Response(status=500)
    
    async def process_message(self, data: dict):
        """메시지 이벤트 처리"""
        message = data.get('message', {})
        chat_id = message.get('chatId')
        person_type = message.get('personType')
        
        if not chat_id:
            return
        
        # 고객 메시지인 경우
        if person_type == 'user' and not message.get('isBot', False):
            user_chat = data.get('userChat', {})
            
            chat_data = {
                'id': chat_id,
                'customerName': user_chat.get('name') or user_chat.get('profile', {}).get('name', '익명'),
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
            
            logger.info(f"새 상담: {chat_data['customerName']}")
        
        # 매니저/봇 답변인 경우
        elif person_type in ['manager', 'bot']:
            await self.remove_chat(chat_id)
            
            # WebSocket 브로드캐스트
            await self.broadcast({
                'type': 'chat_answered',
                'chatId': chat_id
            })
            
            logger.info(f"답변 완료: {chat_id}")
    
    async def process_user_chat(self, data: dict):
        """유저챗 상태 변경 처리"""
        user_chat = data.get('userChat', {})
        chat_id = user_chat.get('id')
        state = user_chat.get('state')
        
        # 상담 종료된 경우
        if state == 'closed' and chat_id:
            await self.remove_chat(chat_id)
            
            await self.broadcast({
                'type': 'chat_answered',
                'chatId': chat_id
            })
            
            logger.info(f"상담 종료: {chat_id}")
    
    async def fetch_channel_api(self, endpoint: str, params: dict = None) -> Optional[dict]:
        """채널톡 API 호출"""
        if not self.channel_api_key or not self.channel_api_secret:
            logger.error("채널톡 API 키가 설정되지 않음")
            return None
        
        url = f"https://api.channel.io/open/v5/{endpoint}"
        headers = {
            "x-access-key": self.channel_api_key,
            "x-access-secret": self.channel_api_secret,
            "Content-Type": "application/json"
        }
        
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, headers=headers, params=params) as response:
                    if response.status == 200:
                        return await response.json()
                    else:
                        logger.error(f"API 호출 실패: {response.status}")
                        return None
        except Exception as e:
            logger.error(f"API 호출 오류: {e}")
            return None
    
    async def initial_sync(self):
        """초기 데이터 동기화"""
        logger.info("초기 동기화 시작")
        
        # 열린 상담 조회
        data = await self.fetch_channel_api('user-chats', {
            'state': 'opened',
            'limit': 500
        })
        
        if not data:
            return
        
        processed = 0
        for chat in data.get('userChats', []):
            chat_id = chat.get('id')
            messages = chat.get('messages', [])
            
            # 마지막 고객 메시지 찾기
            last_customer_msg = None
            has_answer = False
            
            for msg in reversed(messages):
                if msg.get('personType') == 'user' and not msg.get('isBot'):
                    if not last_customer_msg:
                        last_customer_msg = msg
                elif msg.get('personType') in ['manager', 'bot'] and last_customer_msg:
                    has_answer = True
                    break
            
            # 미답변 상담인 경우 저장
            if last_customer_msg and not has_answer:
                chat_data = {
                    'id': chat_id,
                    'customerName': chat.get('name', '익명'),
                    'lastMessage': last_customer_msg.get('plainText', ''),
                    'timestamp': last_customer_msg.get('createdAt'),
                    'waitMinutes': self.calculate_wait_time(last_customer_msg.get('createdAt'))
                }
                await self.save_chat(chat_data)
                processed += 1
        
        logger.info(f"초기 동기화 완료: {processed}개 미답변 상담")
    
    async def periodic_sync(self):
        """주기적 동기화 (5분마다)"""
        while True:
            try:
                await asyncio.sleep(300)  # 5분
                await self.initial_sync()
                
                # 전체 데이터 브로드캐스트
                chats = await self.get_all_chats()
                await self.broadcast({
                    'type': 'bulk_update',
                    'chats': chats
                })
            except Exception as e:
                logger.error(f"주기적 동기화 실패: {e}")
    
    async def get_chats(self, request):
        """미답변 상담 목록 API"""
        chats = await self.get_all_chats()
        
        return web.json_response({
            'chats': chats,
            'total': len(chats),
            'timestamp': datetime.utcnow().isoformat()
        }, headers={
            'Cache-Control': 'no-cache'
        })
    
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
                    # 핑퐁 처리
                    if data.get('type') == 'ping':
                        await ws.send_json({'type': 'pong'})
                elif msg.type == aiohttp.WSMsgType.ERROR:
                    logger.error(f'WebSocket 오류: {ws.exception()}')
        finally:
            # 연결 제거
            self.websockets.discard(ws_ref)
        
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
        
        # 죽은 참조 제거
        self.websockets -= dead_refs
    
    async def health_check(self, request):
        """헬스 체크"""
        redis_status = 'connected' if self.redis else 'disconnected'
        chats = await self.get_all_chats()
        
        return web.json_response({
            'status': 'healthy',
            'redis': redis_status,
            'unanswered_count': len(chats),
            'websocket_connections': len(self.websockets),
            'timestamp': datetime.utcnow().isoformat()
        })
    
    async def serve_dashboard(self, request):
        """대시보드 HTML 서빙"""
        html_path = os.path.join(os.path.dirname(__file__), 'dashboard.html')
        
        if os.path.exists(html_path):
            return web.FileResponse(html_path)
        else:
            # HTML이 없을 경우 인라인으로 제공
            return web.Response(text="Dashboard HTML not found", status=404)

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
        app['sync_task'] = asyncio.create_task(monitor.periodic_sync())
    
    async def cleanup_background_tasks(app):
        app['sync_task'].cancel()
        await app['sync_task']
        await monitor.cleanup()
    
    app.on_startup.append(start_background_tasks)
    app.on_cleanup.append(cleanup_background_tasks)
    
    return app

if __name__ == '__main__':
    loop = asyncio.get_event_loop()
    app = loop.run_until_complete(create_app())
    web.run_app(app, host='0.0.0.0', port=PORT)

# requirements.txt
"""
aiohttp==3.8.5
aioredis==1.3.1
python-dateutil==2.8.2
"""

# Dockerfile
"""
FROM python:3.9-slim

WORKDIR /app

# 의존성 설치
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 애플리케이션 복사
COPY . .

# 헬스체크
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
  CMD python -c "import requests; requests.get('http://localhost:10000/health')"

# 실행
EXPOSE 10000
CMD ["python", "-u", "server.py"]
"""

# render.yaml (Render 배포 설정)
"""
services:
  - type: web
    name: channeltalk-monitor
    env: python
    plan: pro
    buildCommand: pip install -r requirements.txt
    startCommand: python server.py
    envVars:
      - key: REDIS_URL
        fromDatabase:
          name: channeltalk-redis
          property: connectionString
      - key: CHANNEL_API_KEY
        sync: false
      - key: CHANNEL_API_SECRET
        sync: false
      - key: WEBHOOK_TOKEN
        generateValue: true
    healthCheckPath: /health
    autoDeploy: true

databases:
  - name: channeltalk-redis
    plan: pro
    type: redis
"""
