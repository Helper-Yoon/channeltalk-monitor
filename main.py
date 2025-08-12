import asyncio
import aiohttp
from aiohttp import web
import redis.asyncio as aioredis
import json
import os
from datetime import datetime, timezone, timedelta
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
CHANNELTALK_DESK_URL = 'https://desk.channel.io/#/channels/@ajungdang/user_chats/'

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
            await self.redis.aclose()
            
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
                        await self.redis.srem('unanswered_chats', chat_id)
                else:
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
    
    async def remove_chat(self, chat_id: str, manager_name: str = None):
        """채팅 제거 및 답변자 기록"""
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
            
            # 답변자 랭킹 업데이트
            if manager_name:
                today = datetime.now().strftime('%Y-%m-%d')
                await pipe.hincrby('ranking:daily', f"{today}:{manager_name}", 1)
                await pipe.hincrby('ranking:total', manager_name, 1)
            
            results = await pipe.execute()
            
            # 실제로 제거된 경우만 처리
            if results[2]:  # srem 결과 확인
                # 캐시에서 제거
                if chat_id in self.chat_cache:
                    del self.chat_cache[chat_id]
                
                logger.info(f"✅ 제거: {chat_id} {f'(답변자: {manager_name})' if manager_name else ''}")
                
                # WebSocket 브로드캐스트
                await self.broadcast({
                    'type': 'chat_answered',
                    'chatId': chat_id,
                    'total': len(self.chat_cache),
                    'manager': manager_name,
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
    
    async def get_rankings(self) -> dict:
        """답변 랭킹 조회"""
        try:
            # 오늘 랭킹
            today = datetime.now().strftime('%Y-%m-%d')
            daily_pattern = f"{today}:*"
            daily_data = {}
            
            # 일별 랭킹 조회
            cursor = '0'
            while cursor != 0:
                cursor, keys = await self.redis.hscan('ranking:daily', cursor, match=daily_pattern)
                for key, value in keys.items():
                    manager = key.split(':', 1)[1]
                    daily_data[manager] = int(value)
            
            # 전체 랭킹
            total_data = await self.redis.hgetall('ranking:total')
            total_ranking = {k: int(v) for k, v in total_data.items()}
            
            # 정렬
            daily_ranking = sorted(daily_data.items(), key=lambda x: x[1], reverse=True)[:10]
            total_ranking = sorted(total_ranking.items(), key=lambda x: x[1], reverse=True)[:10]
            
            return {
                'daily': daily_ranking,
                'total': total_ranking
            }
        except Exception as e:
            logger.error(f"랭킹 조회 실패: {e}")
            return {'daily': [], 'total': []}
    
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
                manager_info = refers.get('manager', {})
                manager_name = manager_info.get('name', 'Bot' if person_type == 'bot' else 'Unknown')
                await self.remove_chat(str(chat_id), manager_name)
                
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
        rankings = await self.get_rankings()
        
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
            'rankings': rankings,
            'timestamp': datetime.now(timezone.utc).isoformat(),
            'lastSync': self.last_sync
        })
    
    async def mark_answered(self, request):
        """API: 수동 답변 완료 처리"""
        try:
            chat_id = request.match_info.get('chat_id')
            data = await request.json() if request.body_exists else {}
            manager_name = data.get('manager', 'Manual')
            
            if chat_id:
                await self.remove_chat(chat_id, manager_name)
                logger.info(f"✅ 수동 답변 완료: {chat_id} by {manager_name}")
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
            rankings = await self.get_rankings()
            await ws.send_json({
                'type': 'initial',
                'chats': chats,
                'rankings': rankings,
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
                            rankings = await self.get_rankings()
                            await ws.send_json({
                                'type': 'refresh',
                                'chats': chats,
                                'rankings': rankings
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

# ===== 아정당 브랜드 최적화 대시보드 =====
DASHBOARD_HTML = """
<!DOCTYPE html>
<html lang="ko">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>아정당 고객센터 | 실시간 상담 모니터</title>
    <style>
        :root {
            /* 아정당 브랜드 컬러 */
            --ajd-blue: #0066CC;
            --ajd-blue-dark: #0052A3;
            --ajd-blue-light: #4D94FF;
            --ajd-sky: #E6F2FF;
            
            /* 배경색 */
            --bg-primary: #FFFFFF;
            --bg-secondary: #F8FAFB;
            --bg-card: #FFFFFF;
            
            /* 텍스트 */
            --text-primary: #1A1A1A;
            --text-secondary: #6B7280;
            --text-light: #9CA3AF;
            
            /* 상태 색상 */
            --critical: #DC2626;
            --warning: #F59E0B;
            --caution: #EAB308;
            --normal: #0066CC;
            --new: #10B981;
            
            /* 기타 */
            --border: #E5E7EB;
            --shadow: 0 2px 8px rgba(0, 0, 0, 0.06);
            --shadow-hover: 0 4px 16px rgba(0, 102, 204, 0.15);
        }

        * { 
            margin: 0; 
            padding: 0; 
            box-sizing: border-box;
        }

        body {
            font-family: 'Pretendard', -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
            background: var(--bg-secondary);
            color: var(--text-primary);
            min-height: 100vh;
            line-height: 1.6;
        }

        .container {
            max-width: 1600px;
            margin: 0 auto;
            padding: 20px;
        }

        /* 헤더 */
        .header {
            background: var(--bg-primary);
            border-radius: 12px;
            padding: 24px;
            margin-bottom: 24px;
            box-shadow: var(--shadow);
        }

        .header-top {
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 20px;
            flex-wrap: wrap;
            gap: 16px;
        }

        .logo-section {
            display: flex;
            align-items: center;
            gap: 16px;
        }

        .logo {
            width: 40px;
            height: 40px;
            background: var(--ajd-blue);
            border-radius: 8px;
            display: flex;
            align-items: center;
            justify-content: center;
            color: white;
            font-weight: 700;
            font-size: 18px;
        }

        .title {
            font-size: 24px;
            font-weight: 700;
            color: var(--text-primary);
        }

        .header-controls {
            display: flex;
            gap: 12px;
            align-items: center;
        }

        .current-time {
            padding: 8px 16px;
            background: var(--bg-secondary);
            border-radius: 8px;
            font-size: 14px;
            font-weight: 500;
            color: var(--text-secondary);
            font-variant-numeric: tabular-nums;
        }

        .view-toggle {
            display: flex;
            background: var(--bg-secondary);
            border-radius: 8px;
            padding: 4px;
        }

        .view-btn {
            padding: 6px 12px;
            border: none;
            background: transparent;
            color: var(--text-secondary);
            cursor: pointer;
            border-radius: 6px;
            font-size: 14px;
            font-weight: 500;
            transition: all 0.2s;
        }

        .view-btn.active {
            background: var(--ajd-blue);
            color: white;
        }

        /* 통계 카드 */
        .stats-grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(140px, 1fr));
            gap: 12px;
        }

        .stat-card {
            background: var(--bg-secondary);
            border-radius: 8px;
            padding: 16px;
            text-align: center;
            border: 1px solid var(--border);
            transition: transform 0.2s;
        }

        .stat-card:hover {
            transform: translateY(-2px);
        }

        .stat-value {
            font-size: 28px;
            font-weight: 700;
            margin-bottom: 4px;
            font-variant-numeric: tabular-nums;
        }

        .stat-label {
            font-size: 12px;
            color: var(--text-secondary);
            font-weight: 500;
        }

        /* 메인 컨텐츠 영역 */
        .main-content {
            display: grid;
            grid-template-columns: 1fr 320px;
            gap: 24px;
            margin-bottom: 24px;
        }

        @media (max-width: 1200px) {
            .main-content {
                grid-template-columns: 1fr;
            }
        }

        /* 상담 리스트 섹션 */
        .chats-section {
            background: var(--bg-primary);
            border-radius: 12px;
            padding: 20px;
            box-shadow: var(--shadow);
        }

        .section-header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 16px;
            padding-bottom: 12px;
            border-bottom: 1px solid var(--border);
        }

        .section-title {
            font-size: 16px;
            font-weight: 600;
            color: var(--text-primary);
        }

        .filter-group {
            display: flex;
            gap: 8px;
        }

        .filter-btn {
            padding: 6px 12px;
            background: transparent;
            border: 1px solid var(--border);
            color: var(--text-secondary);
            border-radius: 6px;
            cursor: pointer;
            font-size: 13px;
            font-weight: 500;
            transition: all 0.2s;
        }

        .filter-btn:hover {
            border-color: var(--ajd-blue);
            color: var(--ajd-blue);
        }

        .filter-btn.active {
            background: var(--ajd-blue);
            color: white;
            border-color: var(--ajd-blue);
        }

        /* 채팅 그리드/리스트 */
        .chat-container {
            max-height: calc(100vh - 400px);
            overflow-y: auto;
        }

        .chat-grid {
            display: grid;
            grid-template-columns: repeat(auto-fill, minmax(320px, 1fr));
            gap: 16px;
        }

        .chat-list {
            display: flex;
            flex-direction: column;
            gap: 8px;
        }

        /* 채팅 카드 */
        .chat-card {
            background: var(--bg-secondary);
            border: 1px solid var(--border);
            border-radius: 8px;
            padding: 16px;
            cursor: pointer;
            transition: all 0.2s;
            position: relative;
            user-select: none;
        }

        .chat-card:hover {
            border-color: var(--ajd-blue);
            box-shadow: var(--shadow-hover);
            transform: translateY(-2px);
        }

        .chat-card::before {
            content: '';
            position: absolute;
            left: 0;
            top: 0;
            bottom: 0;
            width: 3px;
            border-radius: 8px 0 0 8px;
        }

        .chat-card.critical::before { background: var(--critical); }
        .chat-card.warning::before { background: var(--warning); }
        .chat-card.caution::before { background: var(--caution); }
        .chat-card.normal::before { background: var(--normal); }
        .chat-card.new::before { background: var(--new); }

        /* 리스트 뷰 스타일 */
        .chat-list .chat-card {
            display: flex;
            align-items: center;
            gap: 12px;
            padding: 12px 16px;
        }

        .chat-list .customer-name {
            font-weight: 600;
            min-width: 100px;
        }

        .chat-list .message-preview {
            flex: 1;
            white-space: nowrap;
            overflow: hidden;
            text-overflow: ellipsis;
        }

        .chat-list .wait-time {
            font-weight: 600;
            min-width: 60px;
            text-align: right;
        }

        /* 그리드 뷰 스타일 */
        .chat-grid .customer-name {
            font-size: 15px;
            font-weight: 600;
            color: var(--text-primary);
            margin-bottom: 8px;
        }

        .chat-grid .message-preview {
            color: var(--text-secondary);
            font-size: 13px;
            line-height: 1.4;
            margin-bottom: 12px;
            display: -webkit-box;
            -webkit-line-clamp: 2;
            -webkit-box-orient: vertical;
            overflow: hidden;
        }

        .chat-grid .chat-footer {
            display: flex;
            justify-content: space-between;
            align-items: center;
        }

        .wait-time {
            font-size: 13px;
            font-weight: 600;
            color: var(--text-secondary);
        }

        .wait-time.critical { color: var(--critical); }
        .wait-time.warning { color: var(--warning); }
        .wait-time.caution { color: var(--caution); }
        .wait-time.normal { color: var(--normal); }
        .wait-time.new { color: var(--new); }

        /* 랭킹 섹션 */
        .ranking-section {
            background: var(--bg-primary);
            border-radius: 12px;
            padding: 20px;
            box-shadow: var(--shadow);
            height: fit-content;
        }

        .ranking-tabs {
            display: flex;
            gap: 8px;
            margin-bottom: 16px;
        }

        .ranking-tab {
            flex: 1;
            padding: 8px;
            background: var(--bg-secondary);
            border: 1px solid var(--border);
            border-radius: 6px;
            cursor: pointer;
            text-align: center;
            font-size: 13px;
            font-weight: 500;
            color: var(--text-secondary);
            transition: all 0.2s;
        }

        .ranking-tab.active {
            background: var(--ajd-blue);
            color: white;
            border-color: var(--ajd-blue);
        }

        .ranking-list {
            display: flex;
            flex-direction: column;
            gap: 8px;
        }

        .ranking-item {
            display: flex;
            align-items: center;
            gap: 12px;
            padding: 12px;
            background: var(--bg-secondary);
            border-radius: 8px;
            transition: transform 0.2s;
        }

        .ranking-item:hover {
            transform: translateX(4px);
        }

        .ranking-position {
            width: 24px;
            height: 24px;
            background: var(--ajd-blue);
            color: white;
            border-radius: 50%;
            display: flex;
            align-items: center;
            justify-content: center;
            font-size: 12px;
            font-weight: 700;
        }

        .ranking-position.gold { background: #FFD700; color: #000; }
        .ranking-position.silver { background: #C0C0C0; color: #000; }
        .ranking-position.bronze { background: #CD7F32; color: #FFF; }

        .ranking-name {
            flex: 1;
            font-weight: 500;
            color: var(--text-primary);
        }

        .ranking-count {
            font-weight: 700;
            color: var(--ajd-blue);
        }

        /* 빈 상태 */
        .empty-state {
            text-align: center;
            padding: 60px 20px;
            color: var(--text-secondary);
        }

        .empty-icon {
            font-size: 48px;
            margin-bottom: 16px;
        }

        .empty-title {
            font-size: 18px;
            font-weight: 600;
            margin-bottom: 8px;
            color: var(--text-primary);
        }

        .empty-desc {
            font-size: 14px;
            color: var(--text-light);
        }

        /* 스크롤바 */
        ::-webkit-scrollbar {
            width: 6px;
            height: 6px;
        }

        ::-webkit-scrollbar-track {
            background: var(--bg-secondary);
        }

        ::-webkit-scrollbar-thumb {
            background: var(--border);
            border-radius: 3px;
        }

        ::-webkit-scrollbar-thumb:hover {
            background: var(--ajd-blue);
        }

        /* 토스트 알림 */
        .toast {
            position: fixed;
            bottom: 24px;
            right: 24px;
            padding: 12px 20px;
            background: var(--ajd-blue);
            color: white;
            border-radius: 8px;
            font-size: 14px;
            font-weight: 500;
            box-shadow: 0 4px 12px rgba(0, 102, 204, 0.3);
            transform: translateY(100px);
            opacity: 0;
            transition: all 0.3s;
            z-index: 1000;
        }

        .toast.show {
            transform: translateY(0);
            opacity: 1;
        }
    </style>
</head>
<body>
    <div class="container">
        <!-- 헤더 -->
        <div class="header">
            <div class="header-top">
                <div class="logo-section">
                    <div class="logo">아</div>
                    <h1 class="title">아정당 고객센터 모니터</h1>
                </div>
                <div class="header-controls">
                    <div class="current-time" id="currentTime">-</div>
                    <div class="view-toggle">
                        <button class="view-btn active" data-view="grid">카드</button>
                        <button class="view-btn" data-view="list">리스트</button>
                    </div>
                </div>
            </div>
            
            <!-- 통계 -->
            <div class="stats-grid">
                <div class="stat-card">
                    <div class="stat-value" style="color: var(--ajd-blue)" id="totalCount">0</div>
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

        <!-- 메인 컨텐츠 -->
        <div class="main-content">
            <!-- 상담 리스트 -->
            <div class="chats-section">
                <div class="section-header">
                    <h2 class="section-title">미답변 상담</h2>
                    <div class="filter-group">
                        <button class="filter-btn active" data-filter="all">전체</button>
                        <button class="filter-btn" data-filter="critical">긴급</button>
                        <button class="filter-btn" data-filter="warning">경고</button>
                        <button class="filter-btn" data-filter="caution">주의</button>
                    </div>
                </div>
                <div class="chat-container">
                    <div class="chat-grid" id="chatContainer">
                        <!-- 채팅 카드 동적 생성 -->
                    </div>
                </div>
            </div>

            <!-- 랭킹 -->
            <div class="ranking-section">
                <div class="section-header">
                    <h2 class="section-title">답변 랭킹</h2>
                </div>
                <div class="ranking-tabs">
                    <button class="ranking-tab active" data-ranking="daily">오늘</button>
                    <button class="ranking-tab" data-ranking="total">전체</button>
                </div>
                <div class="ranking-list" id="rankingList">
                    <!-- 랭킹 동적 생성 -->
                </div>
            </div>
        </div>
    </div>

    <div class="toast" id="toast"></div>

    <script>
        const CHANNELTALK_URL = 'https://desk.channel.io/#/channels/@ajungdang/user_chats/';
        
        let ws = null;
        let chats = [];
        let rankings = { daily: [], total: [] };
        let currentFilter = 'all';
        let currentView = 'grid';
        let currentRanking = 'daily';
        let reconnectAttempts = 0;

        // 우선순위 계산
        function getPriority(minutes) {
            if (minutes >= 11) return 'critical';
            if (minutes >= 8) return 'warning';
            if (minutes >= 5) return 'caution';
            if (minutes >= 2) return 'normal';
            return 'new';
        }

        // 대기시간 포맷
        function formatWaitTime(minutes) {
            if (minutes < 1) return '방금';
            if (minutes < 60) return `${Math.floor(minutes)}분`;
            const hours = Math.floor(minutes / 60);
            const mins = minutes % 60;
            return `${hours}시간 ${mins}분`;
        }

        // 토스트 알림
        function showToast(message) {
            const toast = document.getElementById('toast');
            toast.textContent = message;
            toast.classList.add('show');
            setTimeout(() => toast.classList.remove('show'), 3000);
        }

        // 시계 업데이트
        function updateClock() {
            const now = new Date();
            const timeStr = now.toLocaleString('ko-KR', {
                month: 'short',
                day: 'numeric',
                hour: '2-digit',
                minute: '2-digit',
                second: '2-digit',
                hour12: false
            });
            document.getElementById('currentTime').textContent = timeStr;
        }

        // 채팅 렌더링
        function renderChats() {
            const container = document.getElementById('chatContainer');
            container.className = currentView === 'grid' ? 'chat-grid' : 'chat-list';
            
            // 필터링
            let filteredChats = chats;
            if (currentFilter !== 'all') {
                filteredChats = chats.filter(chat => 
                    getPriority(chat.waitMinutes) === currentFilter
                );
            }
            
            if (filteredChats.length === 0) {
                container.innerHTML = `
                    <div class="empty-state">
                        <div class="empty-icon">✨</div>
                        <h3 class="empty-title">대기 중인 상담이 없습니다</h3>
                        <p class="empty-desc">모든 상담이 처리되었습니다</p>
                    </div>
                `;
            } else {
                if (currentView === 'grid') {
                    container.innerHTML = filteredChats.map(chat => {
                        const priority = getPriority(chat.waitMinutes);
                        return `
                            <div class="chat-card ${priority}" ondblclick="openChat('${chat.id}')">
                                <div class="customer-name">${chat.customerName || '익명'}</div>
                                <div class="message-preview">${chat.lastMessage || '(메시지 없음)'}</div>
                                <div class="chat-footer">
                                    <span class="wait-time ${priority}">${formatWaitTime(chat.waitMinutes)}</span>
                                </div>
                            </div>
                        `;
                    }).join('');
                } else {
                    container.innerHTML = filteredChats.map(chat => {
                        const priority = getPriority(chat.waitMinutes);
                        return `
                            <div class="chat-card ${priority}" ondblclick="openChat('${chat.id}')">
                                <div class="customer-name">${chat.customerName || '익명'}</div>
                                <div class="message-preview">${chat.lastMessage || '(메시지 없음)'}</div>
                                <div class="wait-time ${priority}">${formatWaitTime(chat.waitMinutes)}</div>
                            </div>
                        `;
                    }).join('');
                }
            }
            
            updateStats();
        }

        // 통계 업데이트
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

        // 랭킹 렌더링
        function renderRankings() {
            const list = document.getElementById('rankingList');
            const data = rankings[currentRanking] || [];
            
            if (data.length === 0) {
                list.innerHTML = `
                    <div class="empty-state">
                        <p class="empty-desc">아직 데이터가 없습니다</p>
                    </div>
                `;
            } else {
                list.innerHTML = data.slice(0, 10).map((item, index) => {
                    const [name, count] = item;
                    let posClass = '';
                    if (index === 0) posClass = 'gold';
                    else if (index === 1) posClass = 'silver';
                    else if (index === 2) posClass = 'bronze';
                    
                    return `
                        <div class="ranking-item">
                            <div class="ranking-position ${posClass}">${index + 1}</div>
                            <div class="ranking-name">${name}</div>
                            <div class="ranking-count">${count}건</div>
                        </div>
                    `;
                }).join('');
            }
        }

        // 채널톡 열기
        function openChat(chatId) {
            window.open(CHANNELTALK_URL + chatId, '_blank');
        }

        // WebSocket 연결
        function connectWebSocket() {
            const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
            const wsUrl = `${protocol}//${window.location.host}/ws`;
            
            ws = new WebSocket(wsUrl);
            
            ws.onopen = () => {
                console.log('✅ WebSocket 연결됨');
                reconnectAttempts = 0;
            };
            
            ws.onmessage = (event) => {
                const data = JSON.parse(event.data);
                
                if (data.type === 'initial') {
                    chats = data.chats || [];
                    rankings = data.rankings || { daily: [], total: [] };
                    renderChats();
                    renderRankings();
                } else if (data.type === 'new_chat') {
                    if (!chats.find(c => c.id === data.chat.id)) {
                        chats.push(data.chat);
                        chats.sort((a, b) => b.waitMinutes - a.waitMinutes);
                        renderChats();
                        showToast(`새 상담: ${data.chat.customerName}`);
                    }
                } else if (data.type === 'chat_answered') {
                    chats = chats.filter(c => c.id !== data.chatId);
                    renderChats();
                    if (data.manager) {
                        // 랭킹 업데이트 필요
                        fetchData();
                    }
                } else if (data.type === 'refresh') {
                    chats = data.chats || [];
                    rankings = data.rankings || { daily: [], total: [] };
                    renderChats();
                    renderRankings();
                }
            };
            
            ws.onerror = (error) => {
                console.error('WebSocket 오류:', error);
            };
            
            ws.onclose = () => {
                if (reconnectAttempts < 5) {
                    reconnectAttempts++;
                    setTimeout(connectWebSocket, 3000 * reconnectAttempts);
                }
            };
        }

        // 데이터 가져오기
        async function fetchData() {
            try {
                const response = await fetch('/api/chats');
                const data = await response.json();
                chats = data.chats || [];
                rankings = data.rankings || { daily: [], total: [] };
                renderChats();
                renderRankings();
            } catch (error) {
                console.error('데이터 로드 실패:', error);
            }
        }

        // 이벤트 리스너
        document.addEventListener('DOMContentLoaded', () => {
            // 뷰 전환
            document.querySelectorAll('.view-btn').forEach(btn => {
                btn.addEventListener('click', (e) => {
                    document.querySelectorAll('.view-btn').forEach(b => b.classList.remove('active'));
                    e.target.classList.add('active');
                    currentView = e.target.dataset.view;
                    renderChats();
                });
            });

            // 필터
            document.querySelectorAll('.filter-btn').forEach(btn => {
                btn.addEventListener('click', (e) => {
                    document.querySelectorAll('.filter-btn').forEach(b => b.classList.remove('active'));
                    e.target.classList.add('active');
                    currentFilter = e.target.dataset.filter;
                    renderChats();
                });
            });

            // 랭킹 탭
            document.querySelectorAll('.ranking-tab').forEach(btn => {
                btn.addEventListener('click', (e) => {
                    document.querySelectorAll('.ranking-tab').forEach(b => b.classList.remove('active'));
                    e.target.classList.add('active');
                    currentRanking = e.target.dataset.ranking;
                    renderRankings();
                });
            });
        });

        // 초기화
        updateClock();
        setInterval(updateClock, 1000);
        connectWebSocket();
        fetchData();
        setInterval(fetchData, 10000);
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
