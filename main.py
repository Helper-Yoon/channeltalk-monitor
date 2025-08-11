import asyncio
import aiohttp
from aiohttp import web
import redis.asyncio as aioredis
import json
import os
from datetime import datetime, timezone
import logging
from typing import Dict, List, Optional, Set
import weakref
import hashlib
import time
from collections import defaultdict

# ===== 환경 변수 =====
REDIS_URL = os.getenv('REDIS_URL', 'redis://red-d2ct46buibrs738rintg:6379')
WEBHOOK_TOKEN = '80ab2d11835f44b89010c8efa5eec4b4'
PORT = int(os.getenv('PORT', 10000))

# ===== 로깅 설정 =====
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger('ChannelTalk')

# ===== 상수 정의 =====
CACHE_TTL = 86400  # 24시간
PING_INTERVAL = 30  # WebSocket ping 간격
SYNC_INTERVAL = 60  # 데이터 동기화 간격
MAX_RECONNECT_ATTEMPTS = 5
RECONNECT_DELAY = 5

class ChannelTalkMonitor:
    """고성능 Redis 기반 채널톡 모니터링 시스템"""
    
    def __init__(self):
        self.redis: Optional[aioredis.Redis] = None
        self.redis_pool = None
        self.websockets = weakref.WeakSet()
        self.chat_cache: Dict[str, dict] = {}
        self.last_sync = 0
        self.stats = defaultdict(int)
        self._running = False
        self._sync_task = None
        logger.info("🚀 ChannelTalkMonitor 초기화")
        
    async def setup(self):
        """Redis 연결 및 초기화"""
        try:
            # Redis 연결 풀 생성
            self.redis_pool = aioredis.ConnectionPool.from_url(
                REDIS_URL,
                max_connections=50,
                decode_responses=True,
                health_check_interval=30,
                socket_connect_timeout=5,
                retry_on_timeout=True,
                socket_keepalive=True
            )
            
            self.redis = aioredis.Redis(connection_pool=self.redis_pool)
            
            # 연결 테스트
            await self.redis.ping()
            logger.info("✅ Redis 연결 성공!")
            
            # 초기 데이터 로드
            await self._initial_load()
            
            # 동기화 태스크 시작
            self._running = True
            self._sync_task = asyncio.create_task(self._periodic_sync())
            
        except Exception as e:
            logger.error(f"❌ Redis 연결 실패: {e}")
            raise
    
    async def cleanup(self):
        """종료시 정리"""
        self._running = False
        
        if self._sync_task:
            self._sync_task.cancel()
            try:
                await self._sync_task
            except asyncio.CancelledError:
                pass
        
        if self.redis:
            await self.redis.aclose()  # close() 대신 aclose() 사용
            
        if self.redis_pool:
            await self.redis_pool.disconnect()
            
        logger.info("👋 시스템 종료 완료")
    
    async def _initial_load(self):
        """초기 데이터 로드 및 정리"""
        try:
            # 기존 데이터 확인 및 정리
            existing_ids = await self.redis.smembers('unanswered_chats')
            valid_count = 0
            
            for chat_id in existing_ids:
                chat_data = await self.redis.get(f"chat:{chat_id}")
                if chat_data:
                    try:
                        data = json.loads(chat_data)
                        self.chat_cache[chat_id] = data
                        valid_count += 1
                    except:
                        # 손상된 데이터 제거
                        await self.redis.srem('unanswered_chats', chat_id)
                else:
                    # 고아 ID 제거
                    await self.redis.srem('unanswered_chats', chat_id)
            
            logger.info(f"📥 초기 로드: {valid_count}개 미답변 상담")
            
            # 통계 초기화
            await self.redis.hset('stats:session', mapping={
                'start_time': datetime.now(timezone.utc).isoformat(),
                'initial_count': str(valid_count)
            })
            
        except Exception as e:
            logger.error(f"❌ 초기 로드 실패: {e}")
    
    async def _periodic_sync(self):
        """주기적 데이터 동기화"""
        while self._running:
            try:
                await asyncio.sleep(SYNC_INTERVAL)
                await self._sync_data()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"동기화 오류: {e}")
    
    async def _sync_data(self):
        """Redis와 메모리 캐시 동기화"""
        try:
            # 오래된 데이터 정리
            current_time = int(time.time())
            cutoff_time = current_time - CACHE_TTL
            
            removed = await self.redis.zremrangebyscore('chats_by_time', 0, cutoff_time)
            if removed:
                logger.info(f"🧹 {removed}개 오래된 데이터 정리")
            
            # 캐시 동기화
            redis_ids = await self.redis.smembers('unanswered_chats')
            cache_ids = set(self.chat_cache.keys())
            
            # Redis에만 있는 데이터 캐시에 추가
            for chat_id in redis_ids - cache_ids:
                chat_data = await self.redis.get(f"chat:{chat_id}")
                if chat_data:
                    self.chat_cache[chat_id] = json.loads(chat_data)
            
            # 캐시에만 있는 데이터 제거
            for chat_id in cache_ids - redis_ids:
                del self.chat_cache[chat_id]
            
            self.last_sync = current_time
            
        except Exception as e:
            logger.error(f"동기화 실패: {e}")
    
    async def save_chat(self, chat_data: dict):
        """채팅 저장 (중복 방지)"""
        chat_id = str(chat_data['id'])
        
        try:
            # 중복 체크를 위한 해시 생성
            content_hash = hashlib.md5(
                f"{chat_id}:{chat_data.get('lastMessage', '')}:{chat_data.get('timestamp', '')}".encode()
            ).hexdigest()
            
            # 이미 존재하는 경우 업데이트만
            existing_hash = await self.redis.hget(f"chat:{chat_id}:meta", "hash")
            if existing_hash == content_hash:
                logger.debug(f"⏭️ 중복 메시지 스킵: {chat_id}")
                return
            
            # 트랜잭션으로 원자적 처리
            pipe = self.redis.pipeline()
            
            # 메타데이터 저장
            await pipe.hset(f"chat:{chat_id}:meta", mapping={
                "hash": content_hash,
                "updated_at": str(int(time.time()))
            })
            await pipe.expire(f"chat:{chat_id}:meta", CACHE_TTL)
            
            # 채팅 데이터 저장
            await pipe.setex(f"chat:{chat_id}", CACHE_TTL, json.dumps(chat_data))
            
            # 인덱스 업데이트
            await pipe.sadd('unanswered_chats', chat_id)
            score = int(datetime.now(timezone.utc).timestamp())
            await pipe.zadd('chats_by_time', {chat_id: score})
            
            # 통계 업데이트
            await pipe.hincrby('stats:total', 'received', 1)
            await pipe.hincrby('stats:today', f"received:{datetime.now().date()}", 1)
            
            await pipe.execute()
            
            # 캐시 업데이트
            self.chat_cache[chat_id] = chat_data
            
            logger.info(f"✅ 저장: {chat_id} - {chat_data.get('customerName', '익명')}")
            
            # WebSocket 브로드캐스트
            await self.broadcast({
                'type': 'new_chat',
                'chat': chat_data,
                'total': len(self.chat_cache),
                'timestamp': datetime.now(timezone.utc).isoformat()
            })
            
            # 통계 업데이트
            self.stats['saved'] += 1
            
        except Exception as e:
            logger.error(f"❌ 저장 실패 [{chat_id}]: {e}")
    
    async def remove_chat(self, chat_id: str):
        """채팅 제거"""
        chat_id = str(chat_id)
        
        try:
            # 트랜잭션으로 원자적 처리
            pipe = self.redis.pipeline()
            
            # 데이터 제거
            await pipe.delete(f"chat:{chat_id}")
            await pipe.delete(f"chat:{chat_id}:meta")
            await pipe.srem('unanswered_chats', chat_id)
            await pipe.zrem('chats_by_time', chat_id)
            
            # 통계 업데이트
            await pipe.hincrby('stats:total', 'answered', 1)
            await pipe.hincrby('stats:today', f"answered:{datetime.now().date()}", 1)
            
            results = await pipe.execute()
            
            # 실제로 제거된 경우만 처리
            if results[2]:  # srem 결과 확인
                # 캐시에서 제거
                if chat_id in self.chat_cache:
                    del self.chat_cache[chat_id]
                
                logger.info(f"✅ 제거: {chat_id}")
                
                # WebSocket 브로드캐스트
                await self.broadcast({
                    'type': 'chat_answered',
                    'chatId': chat_id,
                    'total': len(self.chat_cache),
                    'timestamp': datetime.now(timezone.utc).isoformat()
                })
                
                self.stats['removed'] += 1
            
        except Exception as e:
            logger.error(f"❌ 제거 실패 [{chat_id}]: {e}")
    
    async def get_all_chats(self) -> List[dict]:
        """모든 미답변 채팅 조회 (캐시 우선)"""
        try:
            # 캐시 우선 사용
            if self.chat_cache:
                chats = list(self.chat_cache.values())
            else:
                # 캐시가 없으면 Redis에서 로드
                chat_ids = await self.redis.zrevrange('chats_by_time', 0, -1)
                
                if not chat_ids:
                    chat_ids = await self.redis.smembers('unanswered_chats')
                
                chats = []
                if chat_ids:
                    pipe = self.redis.pipeline()
                    for chat_id in chat_ids:
                        await pipe.get(f"chat:{chat_id}")
                    
                    results = await pipe.execute()
                    
                    for chat_json in results:
                        if chat_json:
                            try:
                                chats.append(json.loads(chat_json))
                            except:
                                pass
            
            # 대기시간 계산 및 정렬
            current_time = datetime.now(timezone.utc)
            for chat in chats:
                try:
                    if isinstance(chat.get('timestamp'), str):
                        created = datetime.fromisoformat(
                            chat['timestamp'].replace('Z', '+00:00')
                        )
                    else:
                        created = datetime.fromtimestamp(
                            chat['timestamp'] / 1000, 
                            tz=timezone.utc
                        )
                    
                    wait_seconds = (current_time - created).total_seconds()
                    chat['waitMinutes'] = max(0, int(wait_seconds / 60))
                    chat['waitSeconds'] = max(0, int(wait_seconds))
                except:
                    chat['waitMinutes'] = 0
                    chat['waitSeconds'] = 0
            
            # 대기시간 순 정렬
            chats.sort(key=lambda x: x.get('waitSeconds', 0), reverse=True)
            
            return chats
            
        except Exception as e:
            logger.error(f"❌ 조회 실패: {e}")
            return []
    
    async def handle_webhook(self, request):
        """웹훅 처리 (최적화)"""
        # 토큰 검증
        if WEBHOOK_TOKEN not in request.query.getall('token', []):
            logger.warning(f"❌ 잘못된 토큰")
            return web.Response(status=401)
        
        try:
            data = await request.json()
            event_type = data.get('type')
            
            # 비동기 처리로 응답 속도 향상
            if event_type == 'message':
                asyncio.create_task(self.process_message(data))
            elif event_type == 'userChat':
                asyncio.create_task(self.process_user_chat(data))
            
            return web.json_response({"status": "ok"}, status=200)
            
        except Exception as e:
            logger.error(f"웹훅 오류: {e}")
            return web.Response(status=500)
    
    async def process_message(self, data: dict):
        """메시지 처리 (개선)"""
        try:
            entity = data.get('entity', {})
            refers = data.get('refers', {})
            
            chat_id = entity.get('chatId')
            person_type = entity.get('personType')
            
            if not chat_id:
                return
            
            if person_type == 'user':
                # 고객 메시지
                user_info = refers.get('user', {})
                user_chat = refers.get('userChat', {})
                
                chat_data = {
                    'id': str(chat_id),
                    'customerName': (
                        user_info.get('name') or 
                        user_chat.get('name') or 
                        user_info.get('profile', {}).get('name') or 
                        '익명'
                    ),
                    'lastMessage': entity.get('plainText', ''),
                    'timestamp': entity.get('createdAt', datetime.now(timezone.utc).isoformat()),
                    'channel': refers.get('channel', {}).get('name', ''),
                    'tags': refers.get('userChat', {}).get('tags', [])
                }
                
                await self.save_chat(chat_data)
                
            elif person_type in ['manager', 'bot']:
                # 답변시 제거
                await self.remove_chat(str(chat_id))
                
        except Exception as e:
            logger.error(f"메시지 처리 오류: {e}")
    
    async def process_user_chat(self, data: dict):
        """상담 상태 처리"""
        try:
            entity = data.get('entity', {})
            chat_id = entity.get('id')
            state = entity.get('state')
            
            if chat_id and state in ['closed', 'resolved', 'snoozed']:
                await self.remove_chat(str(chat_id))
                
        except Exception as e:
            logger.error(f"상태 처리 오류: {e}")
    
    async def get_chats(self, request):
        """API: 채팅 목록"""
        chats = await self.get_all_chats()
        
        # 통계 수집
        stats = {
            'total': len(chats),
            'critical': len([c for c in chats if c.get('waitMinutes', 0) >= 11]),
            'warning': len([c for c in chats if 8 <= c.get('waitMinutes', 0) < 11]),
            'caution': len([c for c in chats if 5 <= c.get('waitMinutes', 0) < 8]),
            'normal': len([c for c in chats if 2 <= c.get('waitMinutes', 0) < 5]),
            'new': len([c for c in chats if c.get('waitMinutes', 0) < 2]),
            'session': self.stats
        }
        
        return web.json_response({
            'chats': chats,
            'stats': stats,
            'timestamp': datetime.now(timezone.utc).isoformat(),
            'lastSync': self.last_sync
        })
    
    async def mark_answered(self, request):
        """API: 수동 답변 완료 처리"""
        try:
            chat_id = request.match_info.get('chat_id')
            if chat_id:
                await self.remove_chat(chat_id)
                return web.json_response({'status': 'ok', 'chatId': chat_id})
            else:
                return web.json_response({'status': 'error', 'message': 'No chat_id provided'}, status=400)
        except Exception as e:
            logger.error(f"답변 완료 처리 오류: {e}")
            return web.json_response({'status': 'error', 'message': str(e)}, status=500)
    
    async def handle_websocket(self, request):
        """WebSocket 처리 (개선)"""
        ws = web.WebSocketResponse(heartbeat=PING_INTERVAL)
        await ws.prepare(request)
        
        self.websockets.add(ws)
        logger.info(f"🔌 WebSocket 연결 (총 {len(self.websockets)}개)")
        
        try:
            # 초기 데이터 전송
            chats = await self.get_all_chats()
            await ws.send_json({
                'type': 'initial',
                'chats': chats,
                'total': len(chats)
            })
            
            # 메시지 처리
            async for msg in ws:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    try:
                        data = json.loads(msg.data)
                        if data.get('type') == 'ping':
                            await ws.send_json({'type': 'pong'})
                        elif data.get('type') == 'refresh':
                            chats = await self.get_all_chats()
                            await ws.send_json({
                                'type': 'refresh',
                                'chats': chats
                            })
                    except:
                        pass
                elif msg.type in (aiohttp.WSMsgType.ERROR, aiohttp.WSMsgType.CLOSE):
                    break
                    
        except Exception as e:
            logger.error(f"WebSocket 오류: {e}")
        finally:
            self.websockets.discard(ws)
            logger.info(f"🔌 WebSocket 종료 (남은 연결: {len(self.websockets)}개)")
        
        return ws
    
    async def broadcast(self, data: dict):
        """WebSocket 브로드캐스트 (개선)"""
        if not self.websockets:
            return
        
        # 비동기 브로드캐스트
        tasks = []
        for ws in list(self.websockets):
            tasks.append(self._send_to_ws(ws, data))
        
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
    
    async def _send_to_ws(self, ws, data):
        """개별 WebSocket 전송"""
        try:
            await ws.send_json(data)
        except:
            self.websockets.discard(ws)
    
    async def health_check(self, request):
        """헬스 체크"""
        try:
            await self.redis.ping()
            redis_status = 'healthy'
            redis_info = await self.redis.info()
            memory_usage = redis_info.get('used_memory_human', 'N/A')
        except:
            redis_status = 'unhealthy'
            memory_usage = 'N/A'
        
        return web.json_response({
            'status': 'healthy',
            'redis': redis_status,
            'memory': memory_usage,
            'websockets': len(self.websockets),
            'cached_chats': len(self.chat_cache),
            'uptime': int(time.time() - self.stats.get('start_time', time.time())),
            'timestamp': datetime.now(timezone.utc).isoformat()
        })
    
    async def serve_dashboard(self, request):
        """대시보드 HTML 제공"""
        return web.Response(text=DASHBOARD_HTML, content_type='text/html')

# ===== 최적화된 대시보드 HTML =====
DASHBOARD_HTML = """
<!DOCTYPE html>
<html lang="ko">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>아정당 채널톡 실시간 모니터</title>
    <style>
        :root {
            --bg-primary: #0a0e1a;
            --bg-secondary: #151922;
            --bg-card: #1e2330;
            --bg-hover: #252b3b;
            --text-primary: #ffffff;
            --text-secondary: #94a3b8;
            --text-dim: #64748b;
            --border: #2d3548;
            --channeltalk: #5c6ac4;
            --critical: #ef4444;
            --warning: #f97316;
            --caution: #eab308;
            --normal: #3b82f6;
            --new: #10b981;
            --success: #22c55e;
            --glass: rgba(255, 255, 255, 0.05);
        }

        * { 
            margin: 0; 
            padding: 0; 
            box-sizing: border-box;
        }

        body {
            font-family: 'Pretendard', -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
            background: linear-gradient(135deg, var(--bg-primary) 0%, #0f172a 100%);
            color: var(--text-primary);
            min-height: 100vh;
            position: relative;
            overflow-x: hidden;
        }

        /* 배경 애니메이션 */
        body::before {
            content: '';
            position: fixed;
            top: -50%;
            left: -50%;
            width: 200%;
            height: 200%;
            background: radial-gradient(circle at 20% 80%, rgba(92, 106, 196, 0.1) 0%, transparent 50%),
                        radial-gradient(circle at 80% 20%, rgba(239, 68, 68, 0.05) 0%, transparent 50%);
            animation: drift 20s ease-in-out infinite;
            z-index: -1;
        }

        @keyframes drift {
            0%, 100% { transform: rotate(0deg); }
            50% { transform: rotate(180deg); }
        }

        .container {
            max-width: 1600px;
            margin: 0 auto;
            padding: 24px;
        }

        /* 헤더 */
        .header {
            background: var(--glass);
            backdrop-filter: blur(20px);
            border: 1px solid var(--border);
            border-radius: 20px;
            padding: 32px;
            margin-bottom: 32px;
            position: relative;
            overflow: hidden;
        }

        .header::before {
            content: '';
            position: absolute;
            top: 0;
            left: 0;
            right: 0;
            height: 2px;
            background: linear-gradient(90deg, var(--channeltalk), var(--critical), var(--warning), var(--success));
            animation: shimmer 3s linear infinite;
        }

        @keyframes shimmer {
            0% { transform: translateX(-100%); }
            100% { transform: translateX(100%); }
        }

        .header-content {
            display: flex;
            justify-content: space-between;
            align-items: center;
            flex-wrap: wrap;
            gap: 20px;
        }

        .title {
            font-size: 32px;
            font-weight: 800;
            background: linear-gradient(135deg, var(--channeltalk) 0%, #818cf8 100%);
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
            display: flex;
            align-items: center;
            gap: 12px;
        }

        .connection-status {
            display: flex;
            align-items: center;
            gap: 8px;
            padding: 8px 16px;
            background: var(--glass);
            border: 1px solid var(--border);
            border-radius: 12px;
            font-size: 14px;
        }

        .status-dot {
            width: 8px;
            height: 8px;
            border-radius: 50%;
            animation: pulse 2s infinite;
        }

        .status-dot.connected {
            background: var(--success);
        }

        .status-dot.disconnected {
            background: var(--critical);
            animation: none;
        }

        @keyframes pulse {
            0%, 100% { opacity: 1; transform: scale(1); }
            50% { opacity: 0.6; transform: scale(1.2); }
        }

        /* 통계 카드 */
        .stats {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
            gap: 16px;
            margin-top: 24px;
        }

        .stat-card {
            background: var(--glass);
            backdrop-filter: blur(10px);
            border: 1px solid var(--border);
            border-radius: 16px;
            padding: 20px;
            text-align: center;
            transition: all 0.3s cubic-bezier(0.4, 0, 0.2, 1);
            position: relative;
            overflow: hidden;
        }

        .stat-card::before {
            content: '';
            position: absolute;
            top: 0;
            left: 0;
            right: 0;
            bottom: 0;
            background: linear-gradient(135deg, transparent, rgba(255,255,255,0.02));
            opacity: 0;
            transition: opacity 0.3s;
        }

        .stat-card:hover::before {
            opacity: 1;
        }

        .stat-card:hover {
            transform: translateY(-4px);
            border-color: var(--channeltalk);
        }

        .stat-value {
            font-size: 36px;
            font-weight: 800;
            margin-bottom: 4px;
            font-variant-numeric: tabular-nums;
        }

        .stat-label {
            font-size: 13px;
            color: var(--text-secondary);
            text-transform: uppercase;
            letter-spacing: 0.5px;
        }

        /* 필터 & 액션 바 */
        .action-bar {
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 24px;
            gap: 16px;
            flex-wrap: wrap;
        }

        .filter-group {
            display: flex;
            gap: 8px;
            flex-wrap: wrap;
        }

        .filter-btn {
            padding: 8px 16px;
            background: var(--glass);
            border: 1px solid var(--border);
            color: var(--text-secondary);
            border-radius: 10px;
            cursor: pointer;
            font-size: 14px;
            font-weight: 600;
            transition: all 0.2s;
        }

        .filter-btn:hover {
            background: var(--bg-hover);
            color: var(--text-primary);
        }

        .filter-btn.active {
            background: var(--channeltalk);
            color: white;
            border-color: var(--channeltalk);
        }

        .refresh-btn {
            padding: 10px 20px;
            background: linear-gradient(135deg, var(--channeltalk), #818cf8);
            border: none;
            color: white;
            border-radius: 12px;
            cursor: pointer;
            font-size: 14px;
            font-weight: 600;
            transition: all 0.3s;
            display: flex;
            align-items: center;
            gap: 8px;
        }

        .refresh-btn:hover {
            transform: translateY(-2px);
            box-shadow: 0 10px 20px rgba(92, 106, 196, 0.3);
        }

        .refresh-btn:active {
            transform: translateY(0);
        }

        /* 채팅 그리드 */
        .chat-grid {
            display: grid;
            grid-template-columns: repeat(auto-fill, minmax(400px, 1fr));
            gap: 20px;
            animation: fadeIn 0.5s ease-out;
        }

        @keyframes fadeIn {
            from { opacity: 0; transform: translateY(20px); }
            to { opacity: 1; transform: translateY(0); }
        }

        .chat-card {
            background: var(--glass);
            backdrop-filter: blur(10px);
            border: 1px solid var(--border);
            border-radius: 16px;
            padding: 24px;
            position: relative;
            transition: all 0.3s cubic-bezier(0.4, 0, 0.2, 1);
            overflow: hidden;
            animation: slideIn 0.4s ease-out;
        }

        @keyframes slideIn {
            from { 
                opacity: 0; 
                transform: translateX(-20px);
            }
            to { 
                opacity: 1; 
                transform: translateX(0);
            }
        }

        .chat-card::before {
            content: '';
            position: absolute;
            left: 0;
            top: 0;
            bottom: 0;
            width: 4px;
            transition: width 0.3s;
        }

        .chat-card:hover::before {
            width: 6px;
        }

        .chat-card.critical::before { background: linear-gradient(180deg, var(--critical), #dc2626); }
        .chat-card.warning::before { background: linear-gradient(180deg, var(--warning), #ea580c); }
        .chat-card.caution::before { background: linear-gradient(180deg, var(--caution), #d97706); }
        .chat-card.normal::before { background: linear-gradient(180deg, var(--normal), #2563eb); }
        .chat-card.new::before { background: linear-gradient(180deg, var(--new), #059669); }

        .chat-card:hover {
            transform: translateY(-4px) scale(1.02);
            box-shadow: 0 20px 40px rgba(0,0,0,0.3);
            border-color: var(--channeltalk);
        }

        .chat-header {
            display: flex;
            justify-content: space-between;
            align-items: flex-start;
            margin-bottom: 16px;
        }

        .customer-info {
            flex: 1;
        }

        .customer-name {
            font-size: 18px;
            font-weight: 700;
            margin-bottom: 6px;
            color: var(--text-primary);
        }

        .chat-meta {
            display: flex;
            gap: 12px;
            flex-wrap: wrap;
        }

        .wait-badge {
            display: inline-flex;
            align-items: center;
            gap: 4px;
            padding: 6px 12px;
            border-radius: 8px;
            font-size: 13px;
            font-weight: 600;
            animation: pulse 2s infinite;
        }

        .badge-critical { 
            background: linear-gradient(135deg, var(--critical), #dc2626); 
            color: white;
        }
        .badge-warning { 
            background: linear-gradient(135deg, var(--warning), #ea580c); 
            color: white;
        }
        .badge-caution { 
            background: linear-gradient(135deg, var(--caution), #d97706); 
            color: white;
        }
        .badge-normal { 
            background: linear-gradient(135deg, var(--normal), #2563eb); 
            color: white;
        }
        .badge-new { 
            background: linear-gradient(135deg, var(--new), #059669); 
            color: white;
        }

        .message-preview {
            color: var(--text-secondary);
            font-size: 14px;
            line-height: 1.5;
            margin-bottom: 16px;
            max-height: 60px;
            overflow: hidden;
            display: -webkit-box;
            -webkit-line-clamp: 3;
            -webkit-box-orient: vertical;
        }

        .chat-footer {
            display: flex;
            justify-content: space-between;
            align-items: center;
            padding-top: 16px;
            border-top: 1px solid var(--border);
        }

        .chat-tags {
            display: flex;
            gap: 6px;
            flex-wrap: wrap;
            flex: 1;
        }

        .tag {
            padding: 4px 8px;
            background: var(--glass);
            border: 1px solid var(--border);
            border-radius: 6px;
            font-size: 12px;
            color: var(--text-secondary);
        }

        .action-btn {
            padding: 8px 16px;
            background: var(--glass);
            border: 1px solid var(--channeltalk);
            color: var(--channeltalk);
            border-radius: 8px;
            cursor: pointer;
            font-size: 13px;
            font-weight: 600;
            transition: all 0.2s;
        }

        .action-btn:hover {
            background: var(--channeltalk);
            color: white;
            transform: translateY(-2px);
        }

        /* 빈 상태 */
        .empty-state {
            text-align: center;
            padding: 120px 20px;
            color: var(--text-secondary);
            animation: fadeIn 0.5s ease-out;
        }

        .empty-icon {
            font-size: 80px;
            margin-bottom: 24px;
            animation: float 3s ease-in-out infinite;
        }

        @keyframes float {
            0%, 100% { transform: translateY(0); }
            50% { transform: translateY(-10px); }
        }

        .empty-title {
            font-size: 24px;
            font-weight: 700;
            margin-bottom: 8px;
            color: var(--text-primary);
        }

        .empty-desc {
            font-size: 16px;
            color: var(--text-dim);
        }

        /* 알림 */
        .notification {
            position: fixed;
            top: 24px;
            right: 24px;
            padding: 16px 20px;
            background: var(--glass);
            backdrop-filter: blur(20px);
            border: 1px solid var(--border);
            border-radius: 12px;
            color: var(--text-primary);
            font-size: 14px;
            font-weight: 600;
            display: flex;
            align-items: center;
            gap: 12px;
            transform: translateX(400px);
            transition: transform 0.3s cubic-bezier(0.4, 0, 0.2, 1);
            z-index: 1000;
            box-shadow: 0 20px 40px rgba(0,0,0,0.3);
        }

        .notification.show {
            transform: translateX(0);
        }

        .notification.success {
            border-color: var(--success);
            background: linear-gradient(135deg, rgba(34, 197, 94, 0.1), rgba(16, 185, 129, 0.1));
        }

        .notification.error {
            border-color: var(--critical);
            background: linear-gradient(135deg, rgba(239, 68, 68, 0.1), rgba(220, 38, 38, 0.1));
        }

        /* 반응형 */
        @media (max-width: 768px) {
            .container {
                padding: 16px;
            }
            
            .title {
                font-size: 24px;
            }
            
            .chat-grid {
                grid-template-columns: 1fr;
            }
            
            .stats {
                grid-template-columns: repeat(3, 1fr);
            }
        }

        /* 로딩 애니메이션 */
        .loading {
            display: inline-block;
            width: 20px;
            height: 20px;
            border: 3px solid var(--border);
            border-radius: 50%;
            border-top-color: var(--channeltalk);
            animation: spin 1s linear infinite;
        }

        @keyframes spin {
            to { transform: rotate(360deg); }
        }

        /* 스크롤바 스타일 */
        ::-webkit-scrollbar {
            width: 8px;
            height: 8px;
        }

        ::-webkit-scrollbar-track {
            background: var(--bg-primary);
        }

        ::-webkit-scrollbar-thumb {
            background: var(--border);
            border-radius: 4px;
        }

        ::-webkit-scrollbar-thumb:hover {
            background: var(--channeltalk);
        }
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <div class="header-content">
                <h1 class="title">
                    <span>⚡</span>
                    아정당 채널톡 실시간 모니터
                </h1>
                <div class="connection-status">
                    <div class="status-dot" id="statusDot"></div>
                    <span id="statusText">연결 중...</span>
                </div>
            </div>
            
            <div class="stats" id="statsContainer">
                <div class="stat-card">
                    <div class="stat-value" id="totalCount">0</div>
                    <div class="stat-label">전체 대기</div>
                </div>
                <div class="stat-card">
                    <div class="stat-value" style="color: var(--critical)" id="criticalCount">0</div>
                    <div class="stat-label">긴급 (11분+)</div>
                </div>
                <div class="stat-card">
                    <div class="stat-value" style="color: var(--warning)" id="warningCount">0</div>
                    <div class="stat-label">경고 (8-10분)</div>
                </div>
                <div class="stat-card">
                    <div class="stat-value" style="color: var(--caution)" id="cautionCount">0</div>
                    <div class="stat-label">주의 (5-7분)</div>
                </div>
                <div class="stat-card">
                    <div class="stat-value" style="color: var(--normal)" id="normalCount">0</div>
                    <div class="stat-label">일반 (2-4분)</div>
                </div>
                <div class="stat-card">
                    <div class="stat-value" style="color: var(--new)" id="newCount">0</div>
                    <div class="stat-label">신규 (2분 미만)</div>
                </div>
            </div>
        </div>

        <div class="action-bar">
            <div class="filter-group">
                <button class="filter-btn active" data-filter="all">전체</button>
                <button class="filter-btn" data-filter="critical">긴급</button>
                <button class="filter-btn" data-filter="warning">경고</button>
                <button class="filter-btn" data-filter="caution">주의</button>
                <button class="filter-btn" data-filter="normal">일반</button>
                <button class="filter-btn" data-filter="new">신규</button>
            </div>
            <button class="refresh-btn" onclick="refreshData()">
                <span>🔄</span> 새로고침
            </button>
        </div>

        <div class="chat-grid" id="chatGrid">
            <!-- 채팅 카드 동적 생성 -->
        </div>
    </div>

    <div class="notification" id="notification"></div>

    <script>
        let ws = null;
        let chats = [];
        let currentFilter = 'all';
        let reconnectAttempts = 0;
        const MAX_RECONNECT_ATTEMPTS = 5;
        const RECONNECT_DELAY = 3000;
        let soundEnabled = true;

        // 알림음 초기화
        const notificationSound = new Audio('data:audio/wav;base64,UklGRnoGAABXQVZFZm10IBAAAAABAAEAQB8AAEAfAAABAAgAZGF0YQoGAACBhYqFbF1fdJivrJBhNjVgodDbq2EcBj+a2/LDciUFLIHO8tiJNwgZaLvt559NEAxQp+PwtmMcBjiR1/LMeSwFJHfH8N2QQAoUXrTp66hVFApGn+DyvmwhBTGH0fPTgjMGHm7A7+OZURE');

        function getPriority(minutes) {
            if (minutes >= 11) return 'critical';
            if (minutes >= 8) return 'warning';
            if (minutes >= 5) return 'caution';
            if (minutes >= 2) return 'normal';
            return 'new';
        }

        function formatWaitTime(minutes) {
            if (minutes < 1) return '방금 전';
            if (minutes < 60) return `${Math.floor(minutes)}분 대기`;
            const hours = Math.floor(minutes / 60);
            const mins = minutes % 60;
            return `${hours}시간 ${mins}분 대기`;
        }

        function showNotification(message, type = 'info') {
            const notification = document.getElementById('notification');
            notification.textContent = message;
            notification.className = `notification ${type} show`;
            
            if (type === 'success' && soundEnabled) {
                notificationSound.play().catch(() => {});
            }
            
            setTimeout(() => {
                notification.classList.remove('show');
            }, 3000);
        }

        function updateConnectionStatus(connected) {
            const dot = document.getElementById('statusDot');
            const text = document.getElementById('statusText');
            
            if (connected) {
                dot.className = 'status-dot connected';
                text.textContent = '실시간 연결됨';
                reconnectAttempts = 0;
            } else {
                dot.className = 'status-dot disconnected';
                text.textContent = '연결 끊김';
            }
        }

        function renderChats() {
            const grid = document.getElementById('chatGrid');
            
            // 필터링
            let filteredChats = chats;
            if (currentFilter !== 'all') {
                filteredChats = chats.filter(chat => 
                    getPriority(chat.waitMinutes) === currentFilter
                );
            }
            
            if (filteredChats.length === 0) {
                grid.innerHTML = `
                    <div class="empty-state">
                        <div class="empty-icon">✨</div>
                        <h2 class="empty-title">대기 중인 상담이 없습니다</h2>
                        <p class="empty-desc">
                            ${currentFilter !== 'all' ? '선택한 필터에 해당하는' : '현재'} 미답변 상담이 없습니다
                        </p>
                    </div>
                `;
            } else {
                grid.innerHTML = filteredChats.map(chat => {
                    const priority = getPriority(chat.waitMinutes);
                    const tags = chat.tags || [];
                    
                    return `
                        <div class="chat-card ${priority}" data-id="${chat.id}">
                            <div class="chat-header">
                                <div class="customer-info">
                                    <div class="customer-name">${chat.customerName || '익명'}</div>
                                    <div class="chat-meta">
                                        <div class="wait-badge badge-${priority}">
                                            ⏱ ${formatWaitTime(chat.waitMinutes)}
                                        </div>
                                        ${chat.channel ? `<span class="tag">${chat.channel}</span>` : ''}
                                    </div>
                                </div>
                            </div>
                            <div class="message-preview">
                                ${chat.lastMessage || '(메시지 없음)'}
                            </div>
                            <div class="chat-footer">
                                <div class="chat-tags">
                                    ${tags.map(tag => `<span class="tag">#${tag}</span>`).join('')}
                                </div>
                                <button class="action-btn" onclick="markAnswered('${chat.id}')">
                                    답변 완료
                                </button>
                            </div>
                        </div>
                    `;
                }).join('');
            }
            
            updateStats();
        }

        function updateStats() {
            document.getElementById('totalCount').textContent = chats.length;
            document.getElementById('criticalCount').textContent = 
                chats.filter(c => c.waitMinutes >= 11).length;
            document.getElementById('warningCount').textContent = 
                chats.filter(c => c.waitMinutes >= 8 && c.waitMinutes < 11).length;
            document.getElementById('cautionCount').textContent = 
                chats.filter(c => c.waitMinutes >= 5 && c.waitMinutes < 8).length;
            document.getElementById('normalCount').textContent = 
                chats.filter(c => c.waitMinutes >= 2 && c.waitMinutes < 5).length;
            document.getElementById('newCount').textContent = 
                chats.filter(c => c.waitMinutes < 2).length;
        }

        function connectWebSocket() {
            const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
            const wsUrl = `${protocol}//${window.location.host}/ws`;
            
            ws = new WebSocket(wsUrl);
            
            ws.onopen = () => {
                console.log('✅ WebSocket 연결됨');
                updateConnectionStatus(true);
                showNotification('실시간 연결 성공', 'success');
            };
            
            ws.onmessage = (event) => {
                const data = JSON.parse(event.data);
                
                if (data.type === 'initial') {
                    chats = data.chats || [];
                    renderChats();
                } else if (data.type === 'new_chat') {
                    // 중복 체크
                    if (!chats.find(c => c.id === data.chat.id)) {
                        chats.push(data.chat);
                        chats.sort((a, b) => b.waitMinutes - a.waitMinutes);
                        renderChats();
                        showNotification(`새 상담: ${data.chat.customerName}`, 'success');
                    }
                } else if (data.type === 'chat_answered') {
                    chats = chats.filter(c => c.id !== data.chatId);
                    renderChats();
                } else if (data.type === 'refresh') {
                    chats = data.chats || [];
                    renderChats();
                }
            };
            
            ws.onerror = (error) => {
                console.error('WebSocket 오류:', error);
                updateConnectionStatus(false);
            };
            
            ws.onclose = () => {
                updateConnectionStatus(false);
                
                if (reconnectAttempts < MAX_RECONNECT_ATTEMPTS) {
                    reconnectAttempts++;
                    console.log(`재연결 시도 ${reconnectAttempts}/${MAX_RECONNECT_ATTEMPTS}`);
                    setTimeout(connectWebSocket, RECONNECT_DELAY * reconnectAttempts);
                } else {
                    showNotification('연결 실패. 페이지를 새로고침하세요.', 'error');
                }
            };
            
            // 주기적 ping
            setInterval(() => {
                if (ws && ws.readyState === WebSocket.OPEN) {
                    ws.send(JSON.stringify({ type: 'ping' }));
                }
            }, 30000);
        }

        async function fetchData() {
            try {
                const response = await fetch('/api/chats');
                const data = await response.json();
                chats = data.chats || [];
                renderChats();
            } catch (error) {
                console.error('데이터 로드 실패:', error);
                showNotification('데이터 로드 실패', 'error');
            }
        }

        async function refreshData() {
            if (ws && ws.readyState === WebSocket.OPEN) {
                ws.send(JSON.stringify({ type: 'refresh' }));
                showNotification('새로고침 중...', 'info');
            } else {
                await fetchData();
            }
        }

        async function markAnswered(chatId) {
            try {
                await fetch(`/api/chats/${chatId}/answer`, { method: 'POST' });
                chats = chats.filter(c => c.id !== chatId);
                renderChats();
                showNotification('답변 완료 처리됨', 'success');
            } catch (error) {
                console.error('처리 실패:', error);
                showNotification('처리 실패', 'error');
            }
        }

        // 필터 이벤트
        document.addEventListener('DOMContentLoaded', () => {
            document.querySelectorAll('.filter-btn').forEach(btn => {
                btn.addEventListener('click', (e) => {
                    document.querySelectorAll('.filter-btn').forEach(b => 
                        b.classList.remove('active')
                    );
                    e.target.classList.add('active');
                    currentFilter = e.target.dataset.filter;
                    renderChats();
                });
            });
        });

        // 초기화
        connectWebSocket();
        fetchData();
        
        // 정기 동기화 (WebSocket 백업)
        setInterval(() => {
            if (!ws || ws.readyState !== WebSocket.OPEN) {
                fetchData();
            }
        }, 10000);

        // 페이지 가시성 변경 감지
        document.addEventListener('visibilitychange', () => {
            if (!document.hidden) {
                if (!ws || ws.readyState !== WebSocket.OPEN) {
                    connectWebSocket();
                }
                fetchData();
            }
        });

        // 키보드 단축키
        document.addEventListener('keydown', (e) => {
            if (e.key === 'r' && (e.ctrlKey || e.metaKey)) {
                e.preventDefault();
                refreshData();
            }
        });
    </script>
</body>
</html>
"""

# ===== 애플리케이션 생성 =====
async def create_app():
    """애플리케이션 생성 및 설정"""
    logger.info("🏗️ 애플리케이션 초기화 시작")
    
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
            return response
        return middleware_handler
    
    app.middlewares.append(cors_middleware)
    
    # 시작/종료 핸들러
    async def on_startup(app):
        logger.info("=" * 60)
        logger.info("⚡ 아정당 채널톡 실시간 모니터링 시스템")
        logger.info(f"📌 대시보드: http://localhost:{PORT}")
        logger.info(f"🔌 WebSocket: ws://localhost:{PORT}/ws")
        logger.info(f"🎯 웹훅: http://localhost:{PORT}/webhook")
        logger.info("=" * 60)
    
    async def on_cleanup(app):
        await monitor.cleanup()
        logger.info("👋 시스템 종료")
    
    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    
    return app

# ===== 메인 실행 =====
if __name__ == '__main__':
    logger.info("🏁 프로그램 시작")
    
    async def main():
        app = await create_app()
        return app
    
    # 이벤트 루프 실행
    app = asyncio.run(main())
    web.run_app(app, host='0.0.0.0', port=PORT)
