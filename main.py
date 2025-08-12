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
CHANNELTALK_ID = '197228'
CHANNELTALK_DESK_URL = f'https://desk.channel.io/#/channels/{CHANNELTALK_ID}/user_chats/'

# ===== 로깅 설정 =====
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger('ChannelTalk')

# ===== 상수 정의 =====
CACHE_TTL = 43200  # 12시간 (오래된 상담 자동 정리)
PING_INTERVAL = 30  # WebSocket ping 간격
SYNC_INTERVAL = 60  # 데이터 동기화 간격
MAX_RECONNECT_ATTEMPTS = 5
RECONNECT_DELAY = 5

# 팀 설정 (필요에 따라 수정)
TEAMS = {
    'CS': ['김철수', '이영희', '박민수'],
    'Sales': ['최지우', '정하늘', '강바다'],
    'Tech': ['손코딩', '조디버그', '윤서버'],
}

class ChannelTalkMonitor:
    """고성능 Redis 기반 채널톡 모니터링 시스템"""
    
    def __init__(self):
        self.redis: Optional[aioredis.Redis] = None
        self.redis_pool = None
        self.websockets = weakref.WeakSet()
        self.chat_cache: Dict[str, dict] = {}
        self.chat_messages: Dict[str, Set[str]] = {}  # 채팅별 메시지 해시 저장
        self.last_sync = 0
        self.stats = defaultdict(int)
        self._running = False
        self._sync_task = None
        self._cleanup_task = None
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
            
            # 초기 데이터 로드 및 정리
            await self._initial_load()
            
            # 백그라운드 태스크 시작
            self._running = True
            self._sync_task = asyncio.create_task(self._periodic_sync())
            self._cleanup_task = asyncio.create_task(self._periodic_cleanup())
            
        except Exception as e:
            logger.error(f"❌ Redis 연결 실패: {e}")
            raise
    
    async def cleanup(self):
        """종료시 정리"""
        self._running = False
        
        for task in [self._sync_task, self._cleanup_task]:
            if task:
                task.cancel()
                try:
                    await task
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
            removed_count = 0
            
            current_time = datetime.now(timezone.utc)
            
            for chat_id in existing_ids:
                chat_data = await self.redis.get(f"chat:{chat_id}")
                if chat_data:
                    try:
                        data = json.loads(chat_data)
                        # 오래된 상담 체크 (12시간 이상)
                        timestamp = data.get('timestamp')
                        if timestamp:
                            created = datetime.fromisoformat(timestamp.replace('Z', '+00:00'))
                            age_hours = (current_time - created).total_seconds() / 3600
                            
                            if age_hours > 12:
                                # 오래된 상담 제거
                                await self.redis.srem('unanswered_chats', chat_id)
                                await self.redis.delete(f"chat:{chat_id}")
                                removed_count += 1
                                logger.info(f"🧹 오래된 상담 제거: {chat_id} ({age_hours:.1f}시간)")
                            else:
                                self.chat_cache[chat_id] = data
                                valid_count += 1
                        else:
                            self.chat_cache[chat_id] = data
                            valid_count += 1
                    except Exception as e:
                        # 손상된 데이터 제거
                        await self.redis.srem('unanswered_chats', chat_id)
                        await self.redis.delete(f"chat:{chat_id}")
                        logger.error(f"손상된 데이터 제거: {chat_id} - {e}")
                else:
                    # 고아 ID 제거
                    await self.redis.srem('unanswered_chats', chat_id)
                    removed_count += 1
            
            logger.info(f"📥 초기 로드: {valid_count}개 유효 상담, {removed_count}개 제거")
            
            # 통계 초기화
            await self.redis.hset('stats:session', mapping={
                'start_time': datetime.now(timezone.utc).isoformat(),
                'initial_count': str(valid_count),
                'removed_old': str(removed_count)
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
    
    async def _periodic_cleanup(self):
        """주기적 오래된 데이터 정리"""
        while self._running:
            try:
                await asyncio.sleep(3600)  # 1시간마다
                await self._cleanup_old_data()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"정리 작업 오류: {e}")
    
    async def _cleanup_old_data(self):
        """12시간 이상 된 상담 자동 제거"""
        try:
            current_time = datetime.now(timezone.utc)
            removed_count = 0
            
            chat_ids = await self.redis.smembers('unanswered_chats')
            
            for chat_id in chat_ids:
                chat_data = await self.redis.get(f"chat:{chat_id}")
                if chat_data:
                    try:
                        data = json.loads(chat_data)
                        timestamp = data.get('timestamp')
                        if timestamp:
                            created = datetime.fromisoformat(timestamp.replace('Z', '+00:00'))
                            age_hours = (current_time - created).total_seconds() / 3600
                            
                            if age_hours > 12:
                                await self.remove_chat(chat_id, cleanup=True)
                                removed_count += 1
                    except:
                        pass
            
            if removed_count > 0:
                logger.info(f"🧹 정기 정리: {removed_count}개 오래된 상담 제거")
                
        except Exception as e:
            logger.error(f"정리 작업 실패: {e}")
    
    async def _sync_data(self):
        """Redis와 메모리 캐시 동기화"""
        try:
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
                if chat_id in self.chat_messages:
                    del self.chat_messages[chat_id]
            
            self.last_sync = int(time.time())
            
        except Exception as e:
            logger.error(f"동기화 실패: {e}")
    
    async def save_chat(self, chat_data: dict):
        """채팅 저장 (중복 메시지 방지)"""
        chat_id = str(chat_data['id'])
        
        try:
            # 메시지 해시 생성 (중복 체크용)
            message_hash = hashlib.md5(
                f"{chat_data.get('lastMessage', '')}:{chat_data.get('timestamp', '')}".encode()
            ).hexdigest()
            
            # 이 채팅의 메시지 해시 세트 가져오기
            if chat_id not in self.chat_messages:
                self.chat_messages[chat_id] = set()
                # Redis에서 기존 해시 로드
                existing_hashes = await self.redis.smembers(f"chat:{chat_id}:messages")
                if existing_hashes:
                    self.chat_messages[chat_id] = set(existing_hashes)
            
            # 중복 메시지 체크
            if message_hash in self.chat_messages[chat_id]:
                logger.debug(f"⏭️ 중복 메시지 스킵: {chat_id}")
                return
            
            # 새 메시지 해시 추가
            self.chat_messages[chat_id].add(message_hash)
            
            # Redis에 저장
            pipe = self.redis.pipeline()
            
            # 메시지 해시 저장
            await pipe.sadd(f"chat:{chat_id}:messages", message_hash)
            await pipe.expire(f"chat:{chat_id}:messages", CACHE_TTL)
            
            # 채팅 데이터 저장/업데이트
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
            
            self.stats['saved'] += 1
            
        except Exception as e:
            logger.error(f"❌ 저장 실패 [{chat_id}]: {e}")
    
    async def remove_chat(self, chat_id: str, manager_name: str = None, assignee: str = None, cleanup: bool = False):
        """채팅 제거 및 답변자 기록"""
        chat_id = str(chat_id)
        
        try:
            # 트랜잭션으로 원자적 처리
            pipe = self.redis.pipeline()
            
            # 데이터 제거
            await pipe.delete(f"chat:{chat_id}")
            await pipe.delete(f"chat:{chat_id}:messages")
            await pipe.srem('unanswered_chats', chat_id)
            await pipe.zrem('chats_by_time', chat_id)
            
            # 통계 업데이트
            if not cleanup:
                await pipe.hincrby('stats:total', 'answered', 1)
                await pipe.hincrby('stats:today', f"answered:{datetime.now().date()}", 1)
                
                # 답변자 랭킹 업데이트 (Bot 제외, assignee가 아닌 경우만)
                if manager_name and manager_name.lower() != 'bot':
                    if not assignee or manager_name != assignee:
                        today = datetime.now().strftime('%Y-%m-%d')
                        await pipe.hincrby('ranking:daily', f"{today}:{manager_name}", 1)
                        await pipe.hincrby('ranking:total', manager_name, 1)
                        logger.info(f"📊 랭킹 업데이트: {manager_name} (assignee: {assignee})")
            
            results = await pipe.execute()
            
            # 실제로 제거된 경우만 처리
            if results[2]:  # srem 결과 확인
                # 캐시에서 제거
                if chat_id in self.chat_cache:
                    del self.chat_cache[chat_id]
                if chat_id in self.chat_messages:
                    del self.chat_messages[chat_id]
                
                if cleanup:
                    logger.info(f"🧹 오래된 상담 정리: {chat_id}")
                else:
                    logger.info(f"✅ 제거: {chat_id} {f'(답변자: {manager_name})' if manager_name else ''}")
                
                # WebSocket 브로드캐스트
                await self.broadcast({
                    'type': 'chat_answered',
                    'chatId': chat_id,
                    'total': len(self.chat_cache),
                    'manager': manager_name if not cleanup else None,
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
            valid_chats = []
            
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
                    
                    # 12시간 이상된 상담 필터링
                    if wait_seconds > 43200:
                        continue
                    
                    chat['waitMinutes'] = max(0, int(wait_seconds / 60))
                    chat['waitSeconds'] = max(0, int(wait_seconds))
                    valid_chats.append(chat)
                except:
                    chat['waitMinutes'] = 0
                    chat['waitSeconds'] = 0
                    valid_chats.append(chat)
            
            # 대기시간 순 정렬
            valid_chats.sort(key=lambda x: x.get('waitSeconds', 0), reverse=True)
            
            return valid_chats
            
        except Exception as e:
            logger.error(f"❌ 조회 실패: {e}")
            return []
    
    async def get_rankings(self) -> dict:
        """답변 랭킹 조회 (Bot 제외)"""
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
                    # Bot 제외
                    if manager.lower() != 'bot':
                        daily_data[manager] = int(value)
            
            # 전체 랭킹
            total_data = await self.redis.hgetall('ranking:total')
            total_ranking = {}
            for k, v in total_data.items():
                # Bot 제외
                if k.lower() != 'bot':
                    total_ranking[k] = int(v)
            
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
                
                # assignee 정보 추출
                assignee_info = user_chat.get('assignee', {})
                assignee_name = assignee_info.get('name') if assignee_info else None
                
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
                    'tags': refers.get('userChat', {}).get('tags', []),
                    'assignee': assignee_name
                }
                
                await self.save_chat(chat_data)
                
            elif person_type in ['manager', 'bot']:
                # 답변시 제거
                manager_info = refers.get('manager', {})
                manager_name = manager_info.get('name', 'Bot' if person_type == 'bot' else 'Unknown')
                
                # assignee 정보 가져오기
                user_chat = refers.get('userChat', {})
                assignee_info = user_chat.get('assignee', {})
                assignee_name = assignee_info.get('name') if assignee_info else None
                
                await self.remove_chat(str(chat_id), manager_name, assignee_name)
                
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
        
        # 팀별 필터링 (쿼리 파라미터)
        team = request.query.get('team')
        if team and team in TEAMS:
            team_members = TEAMS[team]
            chats = [c for c in chats if c.get('assignee') in team_members]
        
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
            'teams': list(TEAMS.keys()),
            'timestamp': datetime.now(timezone.utc).isoformat(),
            'lastSync': self.last_sync
        })
    
    async def mark_answered(self, request):
        """API: 수동 답변 완료 처리"""
        try:
            chat_id = request.match_info.get('chat_id')
            data = await request.json() if request.body_exists else {}
            manager_name = data.get('manager', 'Manual')
            assignee = data.get('assignee')
            
            if chat_id:
                await self.remove_chat(chat_id, manager_name, assignee)
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
                'teams': list(TEAMS.keys()),
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
                                'rankings': rankings,
                                'teams': list(TEAMS.keys())
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
    <title>채널톡 미답변 상담 모니터링 프로그램</title>
    <style>
        :root {
            /* 아정당 브랜드 컬러 */
            --ajd-blue: #0066CC;
            --ajd-blue-dark: #0052A3;
            --ajd-blue-light: #E6F2FF;
            
            /* 배경색 */
            --bg-primary: #FAFBFC;
            --bg-secondary: #FFFFFF;
            --bg-hover: #F5F7FA;
            
            /* 텍스트 */
            --text-primary: #1A1A1A;
            --text-secondary: #6B7280;
            --text-light: #9CA3AF;
            
            /* 상태 색상 */
            --critical: #DC2626;
            --warning: #F59E0B;
            --caution: #EAB308;
            --normal: #3B82F6;
            --new: #10B981;
            
            /* 기타 */
            --border: #E5E7EB;
            --shadow: 0 1px 3px rgba(0, 0, 0, 0.08);
        }

        * { 
            margin: 0; 
            padding: 0; 
            box-sizing: border-box;
        }

        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
            background: var(--bg-primary);
            color: var(--text-primary);
            min-height: 100vh;
            line-height: 1.5;
        }

        .container {
            max-width: 1920px;
            margin: 0 auto;
            padding: 16px;
        }

        /* 헤더 */
        .header {
            background: var(--bg-secondary);
            border-radius: 8px;
            padding: 20px;
            margin-bottom: 20px;
            box-shadow: var(--shadow);
            border: 1px solid var(--border);
        }

        .header-top {
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 16px;
        }

        .title {
            font-size: 20px;
            font-weight: 600;
            color: var(--text-primary);
        }

        .header-controls {
            display: flex;
            gap: 12px;
            align-items: center;
        }

        .team-selector {
            padding: 6px 12px;
            border: 1px solid var(--border);
            border-radius: 6px;
            background: white;
            font-size: 14px;
            cursor: pointer;
            outline: none;
        }

        .team-selector:focus {
            border-color: var(--ajd-blue);
        }

        .refresh-btn {
            padding: 6px 12px;
            background: var(--ajd-blue);
            color: white;
            border: none;
            border-radius: 6px;
            cursor: pointer;
            font-size: 14px;
            font-weight: 500;
            transition: background 0.2s;
        }

        .refresh-btn:hover {
            background: var(--ajd-blue-dark);
        }

        /* 통계 바 */
        .stats-bar {
            display: flex;
            gap: 8px;
            overflow-x: auto;
            padding: 4px 0;
        }

        .stat-item {
            display: flex;
            align-items: center;
            gap: 8px;
            padding: 8px 16px;
            background: var(--bg-hover);
            border-radius: 6px;
            white-space: nowrap;
            min-width: fit-content;
        }

        .stat-label {
            font-size: 13px;
            color: var(--text-secondary);
        }

        .stat-value {
            font-size: 18px;
            font-weight: 600;
            font-variant-numeric: tabular-nums;
        }

        /* 메인 레이아웃 */
        .main-layout {
            display: grid;
            grid-template-columns: 1fr 300px;
            gap: 20px;
        }

        @media (max-width: 1024px) {
            .main-layout {
                grid-template-columns: 1fr;
            }
        }

        /* 상담 테이블 */
        .chats-section {
            background: var(--bg-secondary);
            border-radius: 8px;
            box-shadow: var(--shadow);
            overflow: hidden;
            border: 1px solid var(--border);
        }

        .table-header {
            padding: 12px 20px;
            border-bottom: 1px solid var(--border);
            background: var(--bg-hover);
            display: flex;
            justify-content: space-between;
            align-items: center;
        }

        .filter-tabs {
            display: flex;
            gap: 4px;
        }

        .filter-tab {
            padding: 4px 12px;
            background: transparent;
            border: 1px solid transparent;
            color: var(--text-secondary);
            border-radius: 4px;
            cursor: pointer;
            font-size: 13px;
            font-weight: 500;
            transition: all 0.2s;
        }

        .filter-tab:hover {
            background: white;
            border-color: var(--border);
        }

        .filter-tab.active {
            background: white;
            color: var(--ajd-blue);
            border-color: var(--ajd-blue);
        }

        /* 테이블 */
        .chat-table {
            width: 100%;
            border-collapse: collapse;
        }

        .chat-table th {
            text-align: left;
            padding: 10px 16px;
            font-size: 12px;
            font-weight: 600;
            color: var(--text-secondary);
            text-transform: uppercase;
            letter-spacing: 0.5px;
            background: var(--bg-hover);
            border-bottom: 1px solid var(--border);
            position: sticky;
            top: 0;
            z-index: 10;
        }

        .chat-table td {
            padding: 12px 16px;
            font-size: 14px;
            border-bottom: 1px solid var(--border);
        }

        .chat-table tr {
            background: white;
            cursor: pointer;
            transition: background 0.1s;
        }

        .chat-table tr:hover {
            background: var(--ajd-blue-light);
        }

        .customer-cell {
            font-weight: 500;
            color: var(--text-primary);
        }

        .message-cell {
            color: var(--text-secondary);
            max-width: 400px;
            overflow: hidden;
            text-overflow: ellipsis;
            white-space: nowrap;
        }

        .time-cell {
            font-weight: 600;
            font-variant-numeric: tabular-nums;
        }

        .time-cell.critical { color: var(--critical); }
        .time-cell.warning { color: var(--warning); }
        .time-cell.caution { color: var(--caution); }
        .time-cell.normal { color: var(--normal); }
        .time-cell.new { color: var(--new); }

        .assignee-cell {
            color: var(--text-secondary);
            font-size: 13px;
        }

        .priority-indicator {
            width: 8px;
            height: 8px;
            border-radius: 50%;
            display: inline-block;
            margin-right: 8px;
        }

        .priority-indicator.critical { background: var(--critical); }
        .priority-indicator.warning { background: var(--warning); }
        .priority-indicator.caution { background: var(--caution); }
        .priority-indicator.normal { background: var(--normal); }
        .priority-indicator.new { background: var(--new); }

        /* 랭킹 섹션 */
        .ranking-section {
            background: var(--bg-secondary);
            border-radius: 8px;
            padding: 16px;
            box-shadow: var(--shadow);
            border: 1px solid var(--border);
            height: fit-content;
        }

        .ranking-header {
            font-size: 14px;
            font-weight: 600;
            margin-bottom: 12px;
            padding-bottom: 8px;
            border-bottom: 1px solid var(--border);
        }

        .ranking-tabs {
            display: grid;
            grid-template-columns: 1fr 1fr;
            gap: 4px;
            margin-bottom: 12px;
        }

        .ranking-tab {
            padding: 6px;
            background: var(--bg-hover);
            border: 1px solid var(--border);
            border-radius: 4px;
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
            gap: 6px;
        }

        .ranking-item {
            display: flex;
            align-items: center;
            gap: 8px;
            padding: 8px;
            background: var(--bg-hover);
            border-radius: 4px;
            font-size: 13px;
        }

        .ranking-position {
            width: 20px;
            height: 20px;
            background: var(--ajd-blue);
            color: white;
            border-radius: 50%;
            display: flex;
            align-items: center;
            justify-content: center;
            font-size: 11px;
            font-weight: 600;
        }

        .ranking-position.gold { background: #FFD700; color: #000; }
        .ranking-position.silver { background: #C0C0C0; color: #000; }
        .ranking-position.bronze { background: #CD7F32; color: #FFF; }

        .ranking-name {
            flex: 1;
            font-weight: 500;
        }

        .ranking-count {
            font-weight: 600;
            color: var(--ajd-blue);
        }

        /* 빈 상태 */
        .empty-state {
            text-align: center;
            padding: 60px 20px;
            color: var(--text-secondary);
        }

        .empty-icon {
            font-size: 36px;
            margin-bottom: 12px;
            opacity: 0.5;
        }

        .empty-message {
            font-size: 14px;
        }

        /* 스크롤바 */
        ::-webkit-scrollbar {
            width: 8px;
            height: 8px;
        }

        ::-webkit-scrollbar-track {
            background: var(--bg-primary);
        }

        ::-webkit-scrollbar-thumb {
            background: #CBD5E1;
            border-radius: 4px;
        }

        ::-webkit-scrollbar-thumb:hover {
            background: #94A3B8;
        }

        /* 테이블 스크롤 컨테이너 */
        .table-container {
            max-height: calc(100vh - 280px);
            overflow-y: auto;
        }
    </style>
</head>
<body>
    <div class="container">
        <!-- 헤더 -->
        <div class="header">
            <div class="header-top">
                <h1 class="title">📊 채널톡 미답변 상담 모니터링 프로그램</h1>
                <div class="header-controls">
                    <select class="team-selector" id="teamSelector">
                        <option value="all">전체 팀</option>
                    </select>
                    <button class="refresh-btn" onclick="refreshData()">새로고침</button>
                </div>
            </div>
            
            <!-- 통계 바 -->
            <div class="stats-bar">
                <div class="stat-item">
                    <span class="stat-label">전체</span>
                    <span class="stat-value" style="color: var(--ajd-blue)" id="totalCount">0</span>
                </div>
                <div class="stat-item">
                    <span class="stat-label">긴급</span>
                    <span class="stat-value" style="color: var(--critical)" id="criticalCount">0</span>
                </div>
                <div class="stat-item">
                    <span class="stat-label">경고</span>
                    <span class="stat-value" style="color: var(--warning)" id="warningCount">0</span>
                </div>
                <div class="stat-item">
                    <span class="stat-label">주의</span>
                    <span class="stat-value" style="color: var(--caution)" id="cautionCount">0</span>
                </div>
                <div class="stat-item">
                    <span class="stat-label">일반</span>
                    <span class="stat-value" style="color: var(--normal)" id="normalCount">0</span>
                </div>
                <div class="stat-item">
                    <span class="stat-label">신규</span>
                    <span class="stat-value" style="color: var(--new)" id="newCount">0</span>
                </div>
            </div>
        </div>

        <!-- 메인 레이아웃 -->
        <div class="main-layout">
            <!-- 상담 테이블 -->
            <div class="chats-section">
                <div class="table-header">
                    <div class="filter-tabs">
                        <button class="filter-tab active" data-filter="all">전체</button>
                        <button class="filter-tab" data-filter="critical">긴급</button>
                        <button class="filter-tab" data-filter="warning">경고</button>
                        <button class="filter-tab" data-filter="caution">주의</button>
                        <button class="filter-tab" data-filter="normal">일반</button>
                        <button class="filter-tab" data-filter="new">신규</button>
                    </div>
                </div>
                <div class="table-container">
                    <table class="chat-table">
                        <thead>
                            <tr>
                                <th style="width: 40px;"></th>
                                <th style="width: 120px;">고객명</th>
                                <th>메시지</th>
                                <th style="width: 100px;">대기시간</th>
                                <th style="width: 100px;">담당자</th>
                            </tr>
                        </thead>
                        <tbody id="chatTableBody">
                            <!-- 동적 생성 -->
                        </tbody>
                    </table>
                </div>
            </div>

            <!-- 랭킹 -->
            <div class="ranking-section">
                <div class="ranking-header">
                    답변 랭킹 (담당 외 상담 답변)
                </div>
                <div class="ranking-tabs">
                    <button class="ranking-tab active" data-ranking="daily">오늘</button>
                    <button class="ranking-tab" data-ranking="total">전체</button>
                </div>
                <div class="ranking-list" id="rankingList">
                    <!-- 동적 생성 -->
                </div>
            </div>
        </div>
    </div>

    <script>
        const CHANNELTALK_URL = 'https://desk.channel.io/#/channels/197228/user_chats/';
        
        let ws = null;
        let chats = [];
        let allChats = [];
        let rankings = { daily: [], total: [] };
        let teams = [];
        let currentFilter = 'all';
        let currentTeam = 'all';
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
            return mins > 0 ? `${hours}시간 ${mins}분` : `${hours}시간`;
        }

        // 테이블 렌더링
        function renderTable() {
            const tbody = document.getElementById('chatTableBody');
            
            // 필터링
            let filteredChats = allChats;
            if (currentFilter !== 'all') {
                filteredChats = filteredChats.filter(chat => 
                    getPriority(chat.waitMinutes) === currentFilter
                );
            }
            
            if (filteredChats.length === 0) {
                tbody.innerHTML = `
                    <tr>
                        <td colspan="5" class="empty-state">
                            <div class="empty-icon">✨</div>
                            <div class="empty-message">현재 대기 중인 상담이 없습니다</div>
                        </td>
                    </tr>
                `;
            } else {
                tbody.innerHTML = filteredChats.map(chat => {
                    const priority = getPriority(chat.waitMinutes);
                    return `
                        <tr ondblclick="openChat('${chat.id}')">
                            <td><span class="priority-indicator ${priority}"></span></td>
                            <td class="customer-cell">${chat.customerName || '익명'}</td>
                            <td class="message-cell">${chat.lastMessage || '(메시지 없음)'}</td>
                            <td class="time-cell ${priority}">${formatWaitTime(chat.waitMinutes)}</td>
                            <td class="assignee-cell">${chat.assignee || '-'}</td>
                        </tr>
                    `;
                }).join('');
            }
            
            updateStats();
        }

        // 통계 업데이트
        function updateStats() {
            const stats = {
                total: allChats.length,
                critical: allChats.filter(c => c.waitMinutes >= 11).length,
                warning: allChats.filter(c => c.waitMinutes >= 8 && c.waitMinutes < 11).length,
                caution: allChats.filter(c => c.waitMinutes >= 5 && c.waitMinutes < 8).length,
                normal: allChats.filter(c => c.waitMinutes >= 2 && c.waitMinutes < 5).length,
                new: allChats.filter(c => c.waitMinutes < 2).length
            };
            
            document.getElementById('totalCount').textContent = stats.total;
            document.getElementById('criticalCount').textContent = stats.critical;
            document.getElementById('warningCount').textContent = stats.warning;
            document.getElementById('cautionCount').textContent = stats.caution;
            document.getElementById('normalCount').textContent = stats.normal;
            document.getElementById('newCount').textContent = stats.new;
        }

        // 랭킹 렌더링
        function renderRankings() {
            const list = document.getElementById('rankingList');
            const data = rankings[currentRanking] || [];
            
            if (data.length === 0) {
                list.innerHTML = `
                    <div class="empty-state">
                        <div class="empty-message">아직 데이터가 없습니다</div>
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

        // 팀 셀렉터 업데이트
        function updateTeamSelector() {
            const selector = document.getElementById('teamSelector');
            const currentValue = selector.value;
            
            selector.innerHTML = '<option value="all">전체 팀</option>';
            teams.forEach(team => {
                selector.innerHTML += `<option value="${team}">${team} 팀</option>`;
            });
            
            selector.value = currentValue;
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
                
                if (data.type === 'initial' || data.type === 'refresh') {
                    chats = data.chats || [];
                    allChats = [...chats];
                    rankings = data.rankings || { daily: [], total: [] };
                    teams = data.teams || [];
                    updateTeamSelector();
                    renderTable();
                    renderRankings();
                } else if (data.type === 'new_chat') {
                    if (!chats.find(c => c.id === data.chat.id)) {
                        chats.push(data.chat);
                        allChats = [...chats];
                        allChats.sort((a, b) => b.waitMinutes - a.waitMinutes);
                        renderTable();
                    }
                } else if (data.type === 'chat_answered') {
                    chats = chats.filter(c => c.id !== data.chatId);
                    allChats = [...chats];
                    renderTable();
                    if (data.manager) {
                        fetchData();
                    }
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
                const url = currentTeam === 'all' ? '/api/chats' : `/api/chats?team=${currentTeam}`;
                const response = await fetch(url);
                const data = await response.json();
                chats = data.chats || [];
                allChats = [...chats];
                rankings = data.rankings || { daily: [], total: [] };
                teams = data.teams || [];
                updateTeamSelector();
                renderTable();
                renderRankings();
            } catch (error) {
                console.error('데이터 로드 실패:', error);
            }
        }

        // 새로고침
        function refreshData() {
            if (ws && ws.readyState === WebSocket.OPEN) {
                ws.send(JSON.stringify({ type: 'refresh' }));
            } else {
                fetchData();
            }
        }

        // 이벤트 리스너
        document.addEventListener('DOMContentLoaded', () => {
            // 필터 탭
            document.querySelectorAll('.filter-tab').forEach(btn => {
                btn.addEventListener('click', (e) => {
                    document.querySelectorAll('.filter-tab').forEach(b => b.classList.remove('active'));
                    e.target.classList.add('active');
                    currentFilter = e.target.dataset.filter;
                    renderTable();
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

            // 팀 셀렉터
            document.getElementById('teamSelector').addEventListener('change', (e) => {
                currentTeam = e.target.value;
                fetchData();
            });
        });

        // 초기화
        connectWebSocket();
        fetchData();
        setInterval(fetchData, 30000); // 30초마다 동기화
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
        logger.info("📊 채널톡 미답변 상담 모니터링 프로그램")
        logger.info(f"📌 대시보드: http://localhost:{PORT}")
        logger.info(f"🔌 WebSocket: ws://localhost:{PORT}/ws")
        logger.info(f"🎯 웹훅: http://localhost:{PORT}/webhook")
        logger.info(f"🆔 채널톡 ID: {CHANNELTALK_ID}")
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
