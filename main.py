import asyncio
import aiohttp
from aiohttp import web
import redis.asyncio as aioredis
from redis.asyncio.connection import ConnectionPool
import json
import os
import hashlib
import hmac
from datetime import datetime, timedelta, timezone
import logging
from typing import Dict, List, Optional, Set, Tuple, Any
import weakref
from dataclasses import dataclass, asdict
from enum import Enum
import time
from collections import defaultdict, deque
import statistics
from concurrent.futures import ThreadPoolExecutor
import multiprocessing
import uvloop  # 고성능 이벤트 루프

# ===== 환경 변수 =====
REDIS_URL = os.getenv('REDIS_URL', 'redis://red-d2ct46buibrs738rintg:6379')
WEBHOOK_TOKEN = os.getenv('WEBHOOK_TOKEN', '80ab2d11835f44b89010c8efa5eec4b4')
API_KEY = os.getenv('API_KEY', '688a26176fcb19aebf8b')
API_SECRET = os.getenv('API_SECRET', 'a0db6c38b95c8ec4d9bb46e7c653b3e2')
PORT = int(os.getenv('PORT', 10000))

# Pro 서버 최적화 설정
CPU_COUNT = multiprocessing.cpu_count()  # 4 CPU 활용
WORKER_COUNT = CPU_COUNT * 2  # 8 워커
MAX_CONNECTIONS = 1000  # Redis 최대 연결
REDIS_POOL_SIZE = 100  # Redis 연결 풀 크기
API_CONCURRENT_LIMIT = 20  # 동시 API 호출 제한
CACHE_SIZE = 10000  # 메모리 캐시 크기

# ===== 로깅 설정 (성능 최적화) =====
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(process)d] %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# ===== 데이터 클래스 =====
class Priority(Enum):
    CRITICAL = "critical"  # 11분 이상
    WARNING = "warning"    # 8-10분
    CAUTION = "caution"    # 5-7분
    NORMAL = "normal"      # 2-4분
    NEW = "new"           # 2분 미만

@dataclass
class ChatMessage:
    id: str
    customer_name: str
    last_message: str
    timestamp: datetime
    wait_minutes: int
    priority: Priority
    channel_id: Optional[str] = None
    tags: List[str] = None
    source: str = "webhook"
    manager_id: Optional[str] = None
    
    def to_dict(self) -> dict:
        return {
            'id': self.id,
            'customerName': self.customer_name,
            'lastMessage': self.last_message,
            'timestamp': self.timestamp.isoformat(),
            'waitMinutes': self.wait_minutes,
            'priority': self.priority.value,
            'channelId': self.channel_id,
            'tags': self.tags or [],
            'source': self.source,
            'managerId': self.manager_id
        }
    
    @classmethod
    def from_dict(cls, data: dict) -> 'ChatMessage':
        return cls(
            id=data['id'],
            customer_name=data.get('customerName', '익명'),
            last_message=data.get('lastMessage', ''),
            timestamp=datetime.fromisoformat(data['timestamp'].replace('Z', '+00:00')),
            wait_minutes=data.get('waitMinutes', 0),
            priority=Priority(data.get('priority', 'new')),
            channel_id=data.get('channelId'),
            tags=data.get('tags', []),
            source=data.get('source', 'webhook'),
            manager_id=data.get('managerId')
        )

# ===== 성능 모니터링 =====
class PerformanceMonitor:
    """시스템 성능 모니터링"""
    
    def __init__(self):
        self.response_times = deque(maxlen=1000)
        self.api_call_times = deque(maxlen=100)
        self.webhook_process_times = deque(maxlen=1000)
        self.websocket_broadcast_times = deque(maxlen=100)
        self.error_count = defaultdict(int)
        self.start_time = time.time()
    
    def record_response_time(self, duration: float):
        self.response_times.append(duration)
    
    def record_api_call(self, duration: float):
        self.api_call_times.append(duration)
    
    def record_webhook_process(self, duration: float):
        self.webhook_process_times.append(duration)
    
    def record_broadcast(self, duration: float):
        self.websocket_broadcast_times.append(duration)
    
    def record_error(self, error_type: str):
        self.error_count[error_type] += 1
    
    def get_stats(self) -> dict:
        uptime = time.time() - self.start_time
        
        return {
            'uptime_seconds': int(uptime),
            'response_time_avg': statistics.mean(self.response_times) if self.response_times else 0,
            'response_time_p95': statistics.quantiles(self.response_times, n=20)[18] if len(self.response_times) > 20 else 0,
            'api_call_avg': statistics.mean(self.api_call_times) if self.api_call_times else 0,
            'webhook_process_avg': statistics.mean(self.webhook_process_times) if self.webhook_process_times else 0,
            'broadcast_avg': statistics.mean(self.websocket_broadcast_times) if self.websocket_broadcast_times else 0,
            'error_counts': dict(self.error_count),
            'total_errors': sum(self.error_count.values())
        }

# ===== 고급 캐싱 시스템 =====
class AdvancedCache:
    """다층 캐싱 시스템"""
    
    def __init__(self, redis_pool: ConnectionPool, max_memory_items: int = CACHE_SIZE):
        self.redis_pool = redis_pool
        self.memory_cache: Dict[str, Tuple[Any, float]] = {}  # (value, timestamp)
        self.max_memory_items = max_memory_items
        self.hit_count = 0
        self.miss_count = 0
        self.memory_ttl = 60  # 메모리 캐시 TTL (초)
    
    async def get(self, key: str) -> Optional[Any]:
        # L1 캐시 (메모리)
        if key in self.memory_cache:
            value, timestamp = self.memory_cache[key]
            if time.time() - timestamp < self.memory_ttl:
                self.hit_count += 1
                return value
            else:
                del self.memory_cache[key]
        
        # L2 캐시 (Redis)
        async with aioredis.Redis(connection_pool=self.redis_pool) as redis:
            value = await redis.get(key)
            if value:
                self.hit_count += 1
                # 메모리 캐시에 저장
                self._update_memory_cache(key, value)
                return json.loads(value) if value else None
        
        self.miss_count += 1
        return None
    
    async def set(self, key: str, value: Any, ttl: int = 3600):
        # 메모리 캐시 업데이트
        self._update_memory_cache(key, value)
        
        # Redis에 저장
        async with aioredis.Redis(connection_pool=self.redis_pool) as redis:
            await redis.setex(key, ttl, json.dumps(value))
    
    def _update_memory_cache(self, key: str, value: Any):
        """LRU 방식으로 메모리 캐시 관리"""
        if len(self.memory_cache) >= self.max_memory_items:
            # 가장 오래된 항목 제거
            oldest_key = min(self.memory_cache.keys(), 
                           key=lambda k: self.memory_cache[k][1])
            del self.memory_cache[oldest_key]
        
        self.memory_cache[key] = (value, time.time())
    
    def get_hit_ratio(self) -> float:
        total = self.hit_count + self.miss_count
        return self.hit_count / total if total > 0 else 0

# ===== 채널톡 API 클라이언트 (병렬 처리 강화) =====
class EnhancedChannelTalkAPI:
    """고성능 채널톡 API 클라이언트"""
    
    def __init__(self, api_key: str, api_secret: str):
        self.api_key = api_key
        self.api_secret = api_secret
        self.base_url = "https://api.channel.io/open/v5"
        self.session: Optional[aiohttp.ClientSession] = None
        self.semaphore = asyncio.Semaphore(API_CONCURRENT_LIMIT)
        self.connector = aiohttp.TCPConnector(
            limit=100,
            limit_per_host=30,
            ttl_dns_cache=300
        )
    
    async def __aenter__(self):
        self.session = aiohttp.ClientSession(
            connector=self.connector,
            timeout=aiohttp.ClientTimeout(total=30)
        )
        return self
    
    async def __aexit__(self, exc_type, exc, tb):
        if self.session:
            await self.session.close()
    
    def _get_headers(self) -> dict:
        return {
            'x-access-key': self.api_key,
            'x-access-secret': self.api_secret,
            'Content-Type': 'application/json'
        }
    
    async def get_open_chats_batch(self, offset: int = 0, limit: int = 100) -> List[dict]:
        """배치로 열린 상담 조회"""
        async with self.semaphore:
            try:
                if not self.session:
                    self.session = aiohttp.ClientSession(connector=self.connector)
                
                url = f"{self.base_url}/user-chats"
                params = {
                    'state': 'opened',
                    'limit': limit,
                    'offset': offset,
                    'sortOrder': 'desc'
                }
                
                async with self.session.get(url, headers=self._get_headers(), params=params) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        return data.get('userChats', [])
                    else:
                        logger.error(f"API 오류: {resp.status}")
                        return []
            except Exception as e:
                logger.error(f"채널톡 API 호출 실패: {e}")
                return []
    
    async def get_all_open_chats(self) -> List[dict]:
        """모든 열린 상담 병렬 조회"""
        all_chats = []
        offset = 0
        batch_size = 100
        
        # 첫 번째 호출로 전체 개수 파악
        first_batch = await self.get_open_chats_batch(0, batch_size)
        all_chats.extend(first_batch)
        
        if len(first_batch) == batch_size:
            # 병렬로 나머지 조회
            tasks = []
            for offset in range(batch_size, 1000, batch_size):  # 최대 1000개
                tasks.append(self.get_open_chats_batch(offset, batch_size))
            
            if tasks:
                results = await asyncio.gather(*tasks, return_exceptions=True)
                for result in results:
                    if isinstance(result, list):
                        all_chats.extend(result)
                        if len(result) < batch_size:
                            break
        
        return all_chats
    
    async def get_chat_messages_batch(self, chat_ids: List[str]) -> Dict[str, List[dict]]:
        """여러 상담의 메시지 병렬 조회"""
        tasks = []
        for chat_id in chat_ids:
            tasks.append(self._get_single_chat_messages(chat_id))
        
        results = await asyncio.gather(*tasks, return_exceptions=True)
        
        messages_dict = {}
        for chat_id, result in zip(chat_ids, results):
            if isinstance(result, list):
                messages_dict[chat_id] = result
            else:
                messages_dict[chat_id] = []
        
        return messages_dict
    
    async def _get_single_chat_messages(self, chat_id: str) -> List[dict]:
        """단일 상담 메시지 조회"""
        async with self.semaphore:
            try:
                if not self.session:
                    self.session = aiohttp.ClientSession(connector=self.connector)
                
                url = f"{self.base_url}/user-chats/{chat_id}/messages"
                params = {'limit': 20, 'sortOrder': 'desc'}
                
                async with self.session.get(url, headers=self._get_headers(), params=params) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        return data.get('messages', [])
                    return []
            except Exception as e:
                logger.error(f"메시지 조회 오류 {chat_id}: {e}")
                return []

# ===== 메인 모니터링 시스템 =====
class EnterpriseChannelTalkMonitor:
    """엔터프라이즈급 채널톡 모니터링 시스템"""
    
    def __init__(self):
        # Redis 연결 풀 (대용량)
        self.redis_pool: Optional[ConnectionPool] = None
        
        # API 클라이언트
        self.api_client = EnhancedChannelTalkAPI(API_KEY, API_SECRET)
        
        # 캐싱 시스템
        self.cache: Optional[AdvancedCache] = None
        
        # WebSocket 관리
        self.websockets = weakref.WeakSet()
        self.websocket_groups: Dict[str, Set] = defaultdict(weakref.WeakSet)
        
        # 성능 모니터링
        self.performance = PerformanceMonitor()
        
        # 상태 관리
        self.processed_messages: Set[str] = set()
        self.chat_states: Dict[str, ChatMessage] = {}
        self.last_api_sync = datetime.now(timezone.utc)
        self.sync_lock = asyncio.Lock()
        
        # 통계
        self.stats = {
            'total_received': 0,
            'total_answered': 0,
            'total_timeout': 0,
            'avg_response_time': 0,
            'peak_concurrent': 0
        }
        
        # 워커 풀
        self.executor = ThreadPoolExecutor(max_workers=WORKER_COUNT)
        
        logger.info(f"🚀 Enterprise 모니터링 시스템 초기화 (CPU: {CPU_COUNT}, Workers: {WORKER_COUNT})")
    
    async def setup(self):
        """시스템 초기화"""
        try:
            # Redis 연결 풀 생성
            self.redis_pool = ConnectionPool.from_url(
                REDIS_URL,
                max_connections=REDIS_POOL_SIZE,
                decode_responses=True,
                socket_keepalive=True,
                socket_keepalive_options={
                    1: 1,  # TCP_KEEPIDLE
                    2: 3,  # TCP_KEEPINTVL  
                    3: 5   # TCP_KEEPCNT
                }
            )
            
            # 연결 테스트
            async with aioredis.Redis(connection_pool=self.redis_pool) as redis:
                await redis.ping()
                logger.info(f"✅ Redis 연결 성공 (Pool Size: {REDIS_POOL_SIZE})")
            
            # 캐시 초기화
            self.cache = AdvancedCache(self.redis_pool)
            
            # 기존 데이터 로드
            await self.load_existing_data()
            
            # 초기 동기화
            await self.sync_with_api()
            
            # 백그라운드 작업 시작
            asyncio.create_task(self.periodic_sync())
            asyncio.create_task(self.cleanup_old_data())
            asyncio.create_task(self.monitor_performance())
            asyncio.create_task(self.calculate_statistics())
            
        except Exception as e:
            logger.error(f"❌ 초기화 실패: {e}")
            raise
    
    async def cleanup(self):
        """종료 정리"""
        self.executor.shutdown(wait=True)
        if self.redis_pool:
            await self.redis_pool.disconnect()
        logger.info("👋 시스템 종료")
    
    # ===== 데이터 관리 =====
    
    async def load_existing_data(self):
        """기존 데이터 로드"""
        try:
            async with aioredis.Redis(connection_pool=self.redis_pool) as redis:
                chat_ids = await redis.smembers('unanswered_chats')
                
                # 병렬로 데이터 로드
                if chat_ids:
                    keys = [f"chat:{chat_id}" for chat_id in chat_ids]
                    values = await redis.mget(keys)
                    
                    for chat_id, value in zip(chat_ids, values):
                        if value:
                            try:
                                data = json.loads(value)
                                chat = ChatMessage.from_dict(data)
                                self.chat_states[chat_id] = chat
                            except Exception as e:
                                logger.error(f"데이터 로드 오류 {chat_id}: {e}")
                
                logger.info(f"📥 {len(self.chat_states)}개 기존 상담 로드")
                
        except Exception as e:
            logger.error(f"데이터 로드 실패: {e}")
    
    def calculate_priority(self, wait_minutes: int) -> Priority:
        """대기시간 기반 우선순위 계산"""
        if wait_minutes >= 11:
            return Priority.CRITICAL
        elif wait_minutes >= 8:
            return Priority.WARNING
        elif wait_minutes >= 5:
            return Priority.CAUTION
        elif wait_minutes >= 2:
            return Priority.NORMAL
        else:
            return Priority.NEW
    
    async def save_chat(self, chat: ChatMessage, from_api: bool = False):
        """상담 저장 (최적화)"""
        start_time = time.time()
        
        # 중복 체크
        message_key = f"{chat.id}_{chat.timestamp.isoformat()}"
        if message_key in self.processed_messages:
            return
        
        self.processed_messages.add(message_key)
        
        # 대기시간 계산
        now = datetime.now(timezone.utc)
        wait_minutes = int((now - chat.timestamp).total_seconds() / 60)
        chat.wait_minutes = max(0, wait_minutes)
        chat.priority = self.calculate_priority(wait_minutes)
        
        # 상태 저장
        self.chat_states[chat.id] = chat
        
        # Redis 저장 (파이프라인 사용)
        async with aioredis.Redis(connection_pool=self.redis_pool) as redis:
            pipe = redis.pipeline()
            
            # 채팅 데이터 저장
            chat_key = f"chat:{chat.id}"
            pipe.setex(chat_key, 86400, json.dumps(chat.to_dict()))
            
            # 집합에 추가
            pipe.sadd('unanswered_chats', chat.id)
            
            # 우선순위별 집합에 추가
            pipe.sadd(f'priority:{chat.priority.value}', chat.id)
            
            # 통계 업데이트
            pipe.hincrby('stats', 'total_received', 1)
            pipe.hincrby('stats:daily', datetime.now().strftime('%Y-%m-%d'), 1)
            
            # 시간대별 통계
            hour = datetime.now().hour
            pipe.hincrby('stats:hourly', str(hour), 1)
            
            await pipe.execute()
        
        # 캐시 업데이트
        await self.cache.set(f"chat:{chat.id}", chat.to_dict())
        
        # 성능 기록
        self.performance.record_response_time(time.time() - start_time)
        
        # 통계 업데이트
        self.stats['total_received'] += 1
        current_count = len(self.chat_states)
        if current_count > self.stats['peak_concurrent']:
            self.stats['peak_concurrent'] = current_count
        
        logger.info(f"💾 저장: {chat.customer_name} - {chat.priority.value} ({chat.wait_minutes}분)")
        
        # 실시간 브로드캐스트
        if not from_api:
            await self.broadcast({
                'type': 'new_chat',
                'chat': chat.to_dict(),
                'stats': await self.get_current_stats()
            })
    
    async def remove_chat(self, chat_id: str, reason: str = 'answered'):
        """상담 제거 (최적화)"""
        start_time = time.time()
        
        if chat_id not in self.chat_states:
            return
        
        chat = self.chat_states[chat_id]
        
        # Redis 파이프라인으로 삭제
        async with aioredis.Redis(connection_pool=self.redis_pool) as redis:
            pipe = redis.pipeline()
            
            # 데이터 삭제
            pipe.delete(f"chat:{chat_id}")
            pipe.srem('unanswered_chats', chat_id)
            pipe.srem(f'priority:{chat.priority.value}', chat_id)
            
            # 응답 시간 계산 및 저장
            response_time = chat.wait_minutes
            pipe.lpush('response_times', response_time)
            pipe.ltrim('response_times', 0, 999)  # 최근 1000개만 유지
            
            # 통계 업데이트
            if reason == 'answered':
                pipe.hincrby('stats', 'total_answered', 1)
                pipe.hincrby('stats:daily:answered', datetime.now().strftime('%Y-%m-%d'), 1)
            elif reason == 'timeout':
                pipe.hincrby('stats', 'total_timeout', 1)
            
            await pipe.execute()
        
        # 캐시 삭제
        await self.cache.set(f"chat:{chat_id}", None, ttl=1)
        
        # 상태 제거
        del self.chat_states[chat_id]
        
        # 성능 기록
        self.performance.record_response_time(time.time() - start_time)
        
        # 통계 업데이트
        if reason == 'answered':
            self.stats['total_answered'] += 1
        elif reason == 'timeout':
            self.stats['total_timeout'] += 1
        
        logger.info(f"✅ 제거: {chat_id} - {reason}")
        
        # 브로드캐스트
        await self.broadcast({
            'type': 'chat_removed',
            'chatId': chat_id,
            'reason': reason,
            'stats': await self.get_current_stats()
        })
    
    async def get_all_chats(self, 
                           priority: Optional[Priority] = None,
                           limit: Optional[int] = None) -> List[dict]:
        """상담 목록 조회 (필터링 지원)"""
        chats = []
        
        # 메모리에서 조회 (빠른 응답)
        for chat in self.chat_states.values():
            if priority and chat.priority != priority:
                continue
            
            # 대기시간 재계산
            now = datetime.now(timezone.utc)
            wait_minutes = int((now - chat.timestamp).total_seconds() / 60)
            chat.wait_minutes = wait_minutes
            chat.priority = self.calculate_priority(wait_minutes)
            
            chats.append(chat.to_dict())
        
        # 정렬 (우선순위 > 대기시간)
        priority_order = {
            Priority.CRITICAL: 0,
            Priority.WARNING: 1,
            Priority.CAUTION: 2,
            Priority.NORMAL: 3,
            Priority.NEW: 4
        }
        
        chats.sort(key=lambda x: (
            priority_order[Priority(x['priority'])],
            -x['waitMinutes']
        ))
        
        if limit:
            chats = chats[:limit]
        
        return chats
    
    # ===== API 동기화 =====
    
    async def sync_with_api(self):
        """API와 전체 동기화 (병렬 처리)"""
        async with self.sync_lock:
            start_time = time.time()
            
            try:
                logger.info("🔄 API 동기화 시작")
                
                async with self.api_client as api:
                    # 모든 열린 상담 조회 (병렬)
                    open_chats = await api.get_all_open_chats()
                    logger.info(f"📊 {len(open_chats)}개 열린 상담 발견")
                    
                    if not open_chats:
                        return
                    
                    # 배치로 메시지 조회
                    chat_ids = [chat['id'] for chat in open_chats]
                    batch_size = 20
                    
                    all_messages = {}
                    for i in range(0, len(chat_ids), batch_size):
                        batch = chat_ids[i:i+batch_size]
                        batch_messages = await api.get_chat_messages_batch(batch)
                        all_messages.update(batch_messages)
                    
                    # 미답변 상담 필터링
                    unanswered_chats = []
                    for chat in open_chats:
                        chat_id = str(chat['id'])
                        messages = all_messages.get(chat_id, [])
                        
                        if messages and self._is_unanswered(messages):
                            last_customer_msg = self._get_last_customer_message(messages)
                            if last_customer_msg:
                                chat_msg = ChatMessage(
                                    id=chat_id,
                                    customer_name=chat.get('name', '익명'),
                                    last_message=last_customer_msg.get('plainText', ''),
                                    timestamp=datetime.fromisoformat(
                                        last_customer_msg.get('createdAt', datetime.now(timezone.utc).isoformat()).replace('Z', '+00:00')
                                    ),
                                    wait_minutes=0,
                                    priority=Priority.NEW,
                                    channel_id=chat.get('channelId'),
                                    tags=chat.get('tags', []),
                                    source='api'
                                )
                                unanswered_chats.append(chat_msg)
                    
                    # 저장
                    save_tasks = []
                    for chat in unanswered_chats:
                        save_tasks.append(self.save_chat(chat, from_api=True))
                    
                    if save_tasks:
                        await asyncio.gather(*save_tasks)
                    
                    # 제거할 상담 확인 (API에 없는 것들)
                    api_chat_ids = {str(chat['id']) for chat in open_chats}
                    current_chat_ids = set(self.chat_states.keys())
                    to_remove = current_chat_ids - api_chat_ids
                    
                    for chat_id in to_remove:
                        await self.remove_chat(chat_id, reason='closed')
                    
                    # 동기화 완료
                    self.last_api_sync = datetime.now(timezone.utc)
                    sync_duration = time.time() - start_time
                    
                    self.performance.record_api_call(sync_duration)
                    
                    logger.info(f"✅ API 동기화 완료: {len(unanswered_chats)}개 미답변, {sync_duration:.2f}초")
                    
                    # 브로드캐스트
                    await self.broadcast({
                        'type': 'sync_complete',
                        'count': len(unanswered_chats),
                        'removed': len(to_remove),
                        'duration': sync_duration,
                        'timestamp': self.last_api_sync.isoformat()
                    })
                    
            except Exception as e:
                logger.error(f"❌ API 동기화 실패: {e}")
                self.performance.record_error('api_sync')
    
    def _is_unanswered(self, messages: List[dict]) -> bool:
        """미답변 여부 확인"""
        for msg in messages:
            person_type = msg.get('personType')
            if person_type == 'user':
                return True
            elif person_type in ['manager', 'bot']:
                return False
        return False
    
    def _get_last_customer_message(self, messages: List[dict]) -> Optional[dict]:
        """마지막 고객 메시지"""
        for msg in messages:
            if msg.get('personType') == 'user':
                return msg
        return None
    
    # ===== 백그라운드 작업 =====
    
    async def periodic_sync(self):
        """주기적 동기화 (적응형)"""
        while True:
            try:
                # 상담 개수에 따라 동기화 주기 조정
                chat_count = len(self.chat_states)
                if chat_count > 50:
                    interval = 60  # 1분
                elif chat_count > 20:
                    interval = 120  # 2분
                else:
                    interval = 180  # 3분
                
                await asyncio.sleep(interval)
                await self.sync_with_api()
                
            except Exception as e:
                logger.error(f"주기적 동기화 오류: {e}")
                await asyncio.sleep(60)
    
    async def cleanup_old_data(self):
        """오래된 데이터 정리"""
        while True:
            try:
                await asyncio.sleep(3600)  # 1시간마다
                
                now = datetime.now(timezone.utc)
                to_remove = []
                
                for chat_id, chat in self.chat_states.items():
                    age_hours = (now - chat.timestamp).total_seconds() / 3600
                    if age_hours > 24:  # 24시간 이상
                        to_remove.append(chat_id)
                
                for chat_id in to_remove:
                    await self.remove_chat(chat_id, reason='timeout')
                
                if to_remove:
                    logger.info(f"🗑️ {len(to_remove)}개 오래된 상담 정리")
                
                # 처리된 메시지 ID 정리
                if len(self.processed_messages) > 10000:
                    self.processed_messages = set(list(self.processed_messages)[-5000:])
                
            except Exception as e:
                logger.error(f"데이터 정리 오류: {e}")
    
    async def monitor_performance(self):
        """성능 모니터링"""
        while True:
            try:
                await asyncio.sleep(60)  # 1분마다
                
                stats = self.performance.get_stats()
                cache_hit_ratio = self.cache.get_hit_ratio() if self.cache else 0
                
                logger.info(f"""
                📊 성능 메트릭:
                - 응답시간 평균: {stats['response_time_avg']:.3f}초
                - 응답시간 P95: {stats['response_time_p95']:.3f}초
                - API 호출 평균: {stats['api_call_avg']:.3f}초
                - 캐시 적중률: {cache_hit_ratio:.2%}
                - WebSocket 연결: {len(self.websockets)}개
                - 총 오류: {stats['total_errors']}
                """)
                
            except Exception as e:
                logger.error(f"성능 모니터링 오류: {e}")
    
    async def calculate_statistics(self):
        """통계 계산"""
        while True:
            try:
                await asyncio.sleep(300)  # 5분마다
                
                async with aioredis.Redis(connection_pool=self.redis_pool) as redis:
                    # 평균 응답시간 계산
                    response_times = await redis.lrange('response_times', 0, -1)
                    if response_times:
                        times = [int(t) for t in response_times]
                        avg_response = statistics.mean(times)
                        await redis.hset('stats', 'avg_response_time', avg_response)
                        self.stats['avg_response_time'] = avg_response
                    
                    # 시간대별 패턴 분석
                    hourly_stats = await redis.hgetall('stats:hourly')
                    if hourly_stats:
                        peak_hour = max(hourly_stats.items(), key=lambda x: int(x[1]))
                        await redis.hset('stats', 'peak_hour', peak_hour[0])
                
            except Exception as e:
                logger.error(f"통계 계산 오류: {e}")
    
    # ===== 웹훅 처리 =====
    
    async def handle_webhook(self, request):
        """웹훅 처리 (최적화)"""
        start_time = time.time()
        
        # 토큰 검증
        token = request.query.get('token')
        if token != WEBHOOK_TOKEN:
            return web.Response(status=401)
        
        try:
            data = await request.json()
            event_type = data.get('type')
            
            # 비동기 처리
            if event_type == 'message':
                asyncio.create_task(self.process_message(data))
            elif event_type == 'userChat':
                asyncio.create_task(self.process_user_chat(data))
            
            # 즉시 응답
            self.performance.record_webhook_process(time.time() - start_time)
            return web.json_response({'status': 'ok'})
            
        except Exception as e:
            logger.error(f"웹훅 처리 오류: {e}")
            self.performance.record_error('webhook')
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
                # 고객 메시지
                user_info = refers.get('user', {})
                user_chat = refers.get('userChat', {})
                
                chat = ChatMessage(
                    id=str(chat_id),
                    customer_name=user_info.get('name') or user_chat.get('name', '익명'),
                    last_message=entity.get('plainText', ''),
                    timestamp=datetime.fromisoformat(
                        entity.get('createdAt', datetime.now(timezone.utc).isoformat()).replace('Z', '+00:00')
                    ),
                    wait_minutes=0,
                    priority=Priority.NEW,
                    channel_id=user_chat.get('channelId'),
                    tags=user_chat.get('tags', []),
                    source='webhook'
                )
                
                await self.save_chat(chat)
                
            elif person_type in ['manager', 'bot']:
                # 답변 처리
                await self.remove_chat(str(chat_id), reason='answered')
                
        except Exception as e:
            logger.error(f"메시지 처리 오류: {e}")
            self.performance.record_error('process_message')
    
    async def process_user_chat(self, data: dict):
        """상담 상태 변경 처리"""
        try:
            entity = data.get('entity', {})
            chat_id = entity.get('id')
            state = entity.get('state')
            
            if state in ['closed', 'resolved'] and chat_id:
                await self.remove_chat(str(chat_id), reason='closed')
                
        except Exception as e:
            logger.error(f"상담 상태 처리 오류: {e}")
            self.performance.record_error('process_user_chat')
    
    # ===== API 엔드포인트 =====
    
    async def get_chats(self, request):
        """상담 목록 API"""
        # 쿼리 파라미터
        priority = request.query.get('priority')
        limit = request.query.get('limit', type=int)
        
        chats = await self.get_all_chats(
            priority=Priority(priority) if priority else None,
            limit=limit
        )
        
        stats = await self.get_current_stats()
        
        return web.json_response({
            'chats': chats,
            'total': len(chats),
            'stats': stats,
            'performance': self.performance.get_stats(),
            'timestamp': datetime.now(timezone.utc).isoformat()
        })
    
    async def get_current_stats(self) -> dict:
        """현재 통계"""
        priority_counts = defaultdict(int)
        for chat in self.chat_states.values():
            priority_counts[chat.priority.value] += 1
        
        return {
            'total': len(self.chat_states),
            'byPriority': dict(priority_counts),
            'totalReceived': self.stats['total_received'],
            'totalAnswered': self.stats['total_answered'],
            'totalTimeout': self.stats['total_timeout'],
            'avgResponseTime': self.stats['avg_response_time'],
            'peakConcurrent': self.stats['peak_concurrent'],
            'cacheHitRatio': self.cache.get_hit_ratio() if self.cache else 0,
            'lastSync': self.last_api_sync.isoformat()
        }
    
    async def mark_answered(self, request):
        """수동 답변 완료"""
        chat_id = request.match_info['chat_id']
        await self.remove_chat(chat_id, reason='answered')
        return web.json_response({'status': 'ok'})
    
    async def force_sync(self, request):
        """강제 동기화"""
        asyncio.create_task(self.sync_with_api())
        return web.json_response({
            'status': 'sync_started',
            'timestamp': datetime.now(timezone.utc).isoformat()
        })
    
    async def get_analytics(self, request):
        """고급 분석 데이터"""
        async with aioredis.Redis(connection_pool=self.redis_pool) as redis:
            # 일별 통계
            daily_stats = {}
            for i in range(7):  # 최근 7일
                date = (datetime.now() - timedelta(days=i)).strftime('%Y-%m-%d')
                received = await redis.hget('stats:daily', date) or 0
                answered = await redis.hget('stats:daily:answered', date) or 0
                daily_stats[date] = {
                    'received': int(received),
                    'answered': int(answered)
                }
            
            # 시간대별 통계
            hourly_stats = await redis.hgetall('stats:hourly')
            
            # 응답시간 분포
            response_times = await redis.lrange('response_times', 0, 99)
            response_dist = {}
            if response_times:
                times = [int(t) for t in response_times]
                response_dist = {
                    'avg': statistics.mean(times),
                    'median': statistics.median(times),
                    'p95': statistics.quantiles(times, n=20)[18] if len(times) > 20 else max(times),
                    'min': min(times),
                    'max': max(times)
                }
        
        return web.json_response({
            'daily': daily_stats,
            'hourly': hourly_stats,
            'responseTimeDistribution': response_dist,
            'performance': self.performance.get_stats(),
            'cacheStats': {
                'hitRatio': self.cache.get_hit_ratio() if self.cache else 0,
                'memoryItems': len(self.cache.memory_cache) if self.cache else 0
            }
        })
    
    async def health_check(self, request):
        """상세 헬스체크"""
        redis_status = 'unknown'
        try:
            async with aioredis.Redis(connection_pool=self.redis_pool) as redis:
                await redis.ping()
                redis_status = 'healthy'
        except:
            redis_status = 'unhealthy'
        
        return web.json_response({
            'status': 'healthy',
            'redis': redis_status,
            'redisPoolSize': REDIS_POOL_SIZE,
            'cpuCount': CPU_COUNT,
            'workerCount': WORKER_COUNT,
            'unansweredCount': len(self.chat_states),
            'websocketConnections': len(self.websockets),
            'processedMessages': len(self.processed_messages),
            'stats': await self.get_current_stats(),
            'performance': self.performance.get_stats(),
            'lastSync': self.last_api_sync.isoformat(),
            'uptime': int(time.time() - self.performance.start_time),
            'timestamp': datetime.now(timezone.utc).isoformat()
        })
    
    # ===== WebSocket =====
    
    async def handle_websocket(self, request):
        """WebSocket 연결 처리"""
        ws = web.WebSocketResponse(heartbeat=30)
        await ws.prepare(request)
        
        # 그룹 관리 (우선순위별 구독 가능)
        groups = request.query.getall('group', [])
        
        self.websockets.add(ws)
        for group in groups:
            self.websocket_groups[group].add(ws)
        
        logger.info(f"🔌 WebSocket 연결 (총 {len(self.websockets)}개)")
        
        try:
            # 초기 데이터
            chats = await self.get_all_chats()
            stats = await self.get_current_stats()
            
            await ws.send_json({
                'type': 'initial',
                'chats': chats,
                'stats': stats,
                'performance': self.performance.get_stats()
            })
            
            # 연결 유지
            async for msg in ws:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    data = json.loads(msg.data)
                    
                    if data.get('type') == 'ping':
                        await ws.send_json({'type': 'pong'})
                    elif data.get('type') == 'subscribe':
                        group = data.get('group')
                        if group:
                            self.websocket_groups[group].add(ws)
                    elif data.get('type') == 'unsubscribe':
                        group = data.get('group')
                        if group:
                            self.websocket_groups[group].discard(ws)
                            
        except Exception as e:
            logger.error(f"WebSocket 오류: {e}")
        finally:
            self.websockets.discard(ws)
            for group_ws in self.websocket_groups.values():
                group_ws.discard(ws)
        
        return ws
    
    async def broadcast(self, data: dict, groups: List[str] = None):
        """WebSocket 브로드캐스트 (그룹 지원)"""
        start_time = time.time()
        
        if groups:
            # 특정 그룹에만 전송
            targets = set()
            for group in groups:
                targets.update(self.websocket_groups.get(group, set()))
        else:
            # 전체 전송
            targets = self.websockets
        
        if not targets:
            return
        
        dead = []
        send_tasks = []
        
        for ws in targets:
            send_tasks.append(self._send_to_websocket(ws, data, dead))
        
        if send_tasks:
            await asyncio.gather(*send_tasks, return_exceptions=True)
        
        # 죽은 연결 제거
        for ws in dead:
            self.websockets.discard(ws)
            for group_ws in self.websocket_groups.values():
                group_ws.discard(ws)
        
        self.performance.record_broadcast(time.time() - start_time)
    
    async def _send_to_websocket(self, ws, data, dead_list):
        """개별 WebSocket 전송"""
        try:
            await ws.send_json(data)
        except:
            dead_list.append(ws)
    
    async def serve_dashboard(self, request):
        """대시보드 HTML"""
        return web.Response(text=ENTERPRISE_DASHBOARD_HTML, content_type='text/html')

# ===== 엔터프라이즈 대시보드 HTML =====
ENTERPRISE_DASHBOARD_HTML = """
<!DOCTYPE html>
<html lang="ko">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>아정당 채널톡 모니터 PRO</title>
    <style>
        :root {
            --bg-primary: #050814;
            --bg-secondary: #0a1628;
            --bg-card: #0f1e35;
            --bg-hover: #14243d;
            --text-primary: #ffffff;
            --text-secondary: #94a3b8;
            --text-dim: #64748b;
            --ajung-blue: #1E6FFF;
            --ajung-light: #4A8FFF;
            --ajung-glow: rgba(30, 111, 255, 0.5);
            --border-color: #1e3a5f;
            --critical: #ef4444;
            --warning: #f97316;
            --caution: #eab308;
            --normal: #3b82f6;
            --new: #10b981;
            --success: #22c55e;
        }

        * {
            margin: 0;
            padding: 0;
            box-sizing: border-box;
        }

        body {
            font-family: 'Inter', -apple-system, BlinkMacSystemFont, system-ui, sans-serif;
            background: linear-gradient(135deg, var(--bg-primary) 0%, #0a0e1a 100%);
            color: var(--text-primary);
            min-height: 100vh;
            position: relative;
        }

        /* 배경 효과 */
        body::before {
            content: '';
            position: fixed;
            top: 0;
            left: 0;
            right: 0;
            bottom: 0;
            background: 
                radial-gradient(circle at 20% 50%, var(--ajung-glow) 0%, transparent 50%),
                radial-gradient(circle at 80% 80%, rgba(74, 143, 255, 0.3) 0%, transparent 50%);
            pointer-events: none;
            opacity: 0.3;
        }

        /* 헤더 */
        .header {
            background: rgba(15, 30, 53, 0.9);
            backdrop-filter: blur(20px);
            border-bottom: 1px solid var(--border-color);
            position: sticky;
            top: 0;
            z-index: 1000;
            box-shadow: 0 4px 20px rgba(0, 0, 0, 0.5);
        }

        .header-content {
            max-width: 1600px;
            margin: 0 auto;
            padding: 20px 24px;
            display: flex;
            justify-content: space-between;
            align-items: center;
        }

        .logo-section {
            display: flex;
            align-items: center;
            gap: 20px;
        }

        .logo {
            width: 140px;
            height: 48px;
            background: linear-gradient(135deg, var(--ajung-blue), var(--ajung-light));
            border-radius: 12px;
            display: flex;
            align-items: center;
            justify-content: center;
            font-weight: 800;
            font-size: 20px;
            letter-spacing: -0.5px;
            box-shadow: 0 4px 15px var(--ajung-glow);
        }

        .title {
            font-size: 24px;
            font-weight: 700;
            background: linear-gradient(135deg, var(--text-primary), var(--ajung-light));
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
        }

        .header-stats {
            display: flex;
            gap: 24px;
            align-items: center;
        }

        .header-stat {
            text-align: center;
        }

        .header-stat-value {
            font-size: 24px;
            font-weight: 700;
            color: var(--ajung-light);
        }

        .header-stat-label {
            font-size: 11px;
            color: var(--text-secondary);
            text-transform: uppercase;
            letter-spacing: 0.5px;
        }

        .sync-indicator {
            display: flex;
            align-items: center;
            gap: 10px;
            padding: 10px 20px;
            background: rgba(30, 111, 255, 0.1);
            border: 1px solid rgba(30, 111, 255, 0.3);
            border-radius: 30px;
        }

        .sync-dot {
            width: 10px;
            height: 10px;
            background: var(--success);
            border-radius: 50%;
            animation: pulse 2s infinite;
        }

        @keyframes pulse {
            0%, 100% { 
                opacity: 1;
                transform: scale(1);
            }
            50% { 
                opacity: 0.5;
                transform: scale(1.2);
            }
        }

        /* 메인 컨테이너 */
        .container {
            max-width: 1600px;
            margin: 0 auto;
            padding: 24px;
            position: relative;
            z-index: 1;
        }

        /* 메트릭 카드 */
        .metrics-grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(250px, 1fr));
            gap: 20px;
            margin-bottom: 32px;
        }

        .metric-card {
            background: rgba(15, 30, 53, 0.6);
            backdrop-filter: blur(10px);
            border: 1px solid var(--border-color);
            border-radius: 16px;
            padding: 24px;
            position: relative;
            overflow: hidden;
            transition: all 0.3s;
        }

        .metric-card:hover {
            transform: translateY(-2px);
            box-shadow: 0 10px 30px rgba(30, 111, 255, 0.2);
            border-color: var(--ajung-blue);
        }

        .metric-card::before {
            content: '';
            position: absolute;
            top: 0;
            left: 0;
            right: 0;
            height: 3px;
            background: linear-gradient(90deg, transparent, var(--ajung-blue), transparent);
            animation: shimmer 3s infinite;
        }

        @keyframes shimmer {
            0% { transform: translateX(-100%); }
            100% { transform: translateX(100%); }
        }

        .metric-icon {
            width: 40px;
            height: 40px;
            background: linear-gradient(135deg, var(--ajung-blue), var(--ajung-light));
            border-radius: 10px;
            display: flex;
            align-items: center;
            justify-content: center;
            font-size: 20px;
            margin-bottom: 16px;
        }

        .metric-label {
            font-size: 12px;
            color: var(--text-secondary);
            text-transform: uppercase;
            letter-spacing: 0.5px;
            margin-bottom: 8px;
        }

        .metric-value {
            font-size: 36px;
            font-weight: 700;
            line-height: 1;
            margin-bottom: 8px;
        }

        .metric-change {
            font-size: 13px;
            color: var(--text-dim);
            display: flex;
            align-items: center;
            gap: 4px;
        }

        .metric-change.positive {
            color: var(--success);
        }

        .metric-change.negative {
            color: var(--critical);
        }

        /* 우선순위 통계 */
        .priority-stats {
            display: grid;
            grid-template-columns: repeat(5, 1fr);
            gap: 16px;
            margin-bottom: 32px;
            padding: 20px;
            background: rgba(15, 30, 53, 0.4);
            border-radius: 16px;
            border: 1px solid var(--border-color);
        }

        .priority-stat {
            text-align: center;
            padding: 16px;
            border-radius: 12px;
            background: rgba(0, 0, 0, 0.2);
            transition: all 0.3s;
        }

        .priority-stat:hover {
            transform: scale(1.05);
        }

        .priority-stat.critical { border-left: 4px solid var(--critical); }
        .priority-stat.warning { border-left: 4px solid var(--warning); }
        .priority-stat.caution { border-left: 4px solid var(--caution); }
        .priority-stat.normal { border-left: 4px solid var(--normal); }
        .priority-stat.new { border-left: 4px solid var(--new); }

        .priority-count {
            font-size: 48px;
            font-weight: 700;
            margin-bottom: 8px;
        }

        .priority-label {
            font-size: 13px;
            color: var(--text-secondary);
        }

        /* 컨트롤 바 */
        .control-bar {
            display: flex;
            justify-content: space-between;
            align-items: center;
            gap: 16px;
            margin-bottom: 24px;
            flex-wrap: wrap;
        }

        .control-group {
            display: flex;
            gap: 12px;
        }

        .btn {
            padding: 12px 24px;
            background: linear-gradient(135deg, var(--ajung-blue), var(--ajung-light));
            border: none;
            color: white;
            border-radius: 10px;
            font-size: 14px;
            font-weight: 600;
            cursor: pointer;
            transition: all 0.3s;
            position: relative;
            overflow: hidden;
        }

        .btn::before {
            content: '';
            position: absolute;
            top: 50%;
            left: 50%;
            width: 0;
            height: 0;
            background: rgba(255, 255, 255, 0.3);
            border-radius: 50%;
            transform: translate(-50%, -50%);
            transition: width 0.6s, height 0.6s;
        }

        .btn:hover::before {
            width: 400px;
            height: 400px;
        }

        .btn:hover {
            transform: translateY(-2px);
            box-shadow: 0 10px 25px var(--ajung-glow);
        }

        .btn-secondary {
            background: var(--bg-card);
            border: 1px solid var(--border-color);
        }

        .btn-secondary:hover {
            border-color: var(--ajung-blue);
            background: var(--bg-hover);
        }

        /* 필터 */
        .filter-tabs {
            display: flex;
            gap: 8px;
            padding: 4px;
            background: rgba(0, 0, 0, 0.3);
            border-radius: 10px;
        }

        .filter-tab {
            padding: 8px 16px;
            background: transparent;
            border: none;
            color: var(--text-secondary);
            border-radius: 8px;
            font-size: 13px;
            font-weight: 600;
            cursor: pointer;
            transition: all 0.3s;
        }

        .filter-tab:hover {
            background: rgba(30, 111, 255, 0.1);
        }

        .filter-tab.active {
            background: var(--ajung-blue);
            color: white;
        }

        /* 채팅 그리드 */
        .chat-grid {
            display: grid;
            grid-template-columns: repeat(auto-fill, minmax(400px, 1fr));
            gap: 20px;
            margin-bottom: 32px;
        }

        .chat-card {
            background: rgba(15, 30, 53, 0.8);
            backdrop-filter: blur(10px);
            border: 1px solid var(--border-color);
            border-radius: 16px;
            padding: 24px;
            position: relative;
            transition: all 0.3s cubic-bezier(0.4, 0, 0.2, 1);
            cursor: pointer;
            animation: slideIn 0.5s ease-out;
            overflow: hidden;
        }

        .chat-card::after {
            content: '';
            position: absolute;
            top: 0;
            left: -100%;
            width: 100%;
            height: 100%;
            background: linear-gradient(90deg, transparent, rgba(30, 111, 255, 0.1), transparent);
            transition: left 0.5s;
        }

        .chat-card:hover::after {
            left: 100%;
        }

        .chat-card:hover {
            transform: translateY(-4px) scale(1.02);
            box-shadow: 0 20px 40px rgba(30, 111, 255, 0.2);
            border-color: var(--ajung-blue);
        }

        .priority-indicator {
            position: absolute;
            left: 0;
            top: 0;
            bottom: 0;
            width: 4px;
            border-radius: 16px 0 0 16px;
        }

        .priority-critical { background: var(--critical); }
        .priority-warning { background: var(--warning); }
        .priority-caution { background: var(--caution); }
        .priority-normal { background: var(--normal); }
        .priority-new { background: var(--new); }

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
            font-weight: 600;
            margin-bottom: 6px;
        }

        .chat-meta {
            display: flex;
            gap: 8px;
            flex-wrap: wrap;
        }

        .meta-tag {
            padding: 3px 10px;
            background: rgba(30, 111, 255, 0.1);
            border: 1px solid rgba(30, 111, 255, 0.2);
            border-radius: 6px;
            font-size: 11px;
            color: var(--ajung-light);
        }

        .wait-badge {
            padding: 8px 16px;
            border-radius: 10px;
            font-size: 14px;
            font-weight: 600;
            white-space: nowrap;
            display: flex;
            align-items: center;
            gap: 6px;
        }

        .message-content {
            color: var(--text-secondary);
            font-size: 14px;
            line-height: 1.6;
            margin-bottom: 16px;
            max-height: 80px;
            overflow: hidden;
            position: relative;
        }

        .chat-footer {
            display: flex;
            justify-content: space-between;
            align-items: center;
            padding-top: 16px;
            border-top: 1px solid rgba(255, 255, 255, 0.05);
        }

        .chat-time {
            font-size: 12px;
            color: var(--text-dim);
        }

        .chat-actions {
            display: flex;
            gap: 8px;
        }

        .action-btn {
            padding: 6px 16px;
            background: var(--ajung-blue);
            border: none;
            color: white;
            border-radius: 8px;
            font-size: 13px;
            font-weight: 600;
            cursor: pointer;
            transition: all 0.2s;
        }

        .action-btn:hover {
            background: var(--ajung-light);
            transform: scale(1.05);
        }

        /* 퍼포먼스 모니터 */
        .performance-panel {
            position: fixed;
            bottom: 24px;
            right: 24px;
            background: rgba(15, 30, 53, 0.95);
            backdrop-filter: blur(10px);
            border: 1px solid var(--border-color);
            border-radius: 16px;
            padding: 20px;
            min-width: 300px;
            box-shadow: 0 10px 40px rgba(0, 0, 0, 0.5);
            transition: all 0.3s;
        }

        .performance-panel.collapsed {
            padding: 12px;
            min-width: auto;
        }

        .performance-header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 16px;
        }

        .performance-title {
            font-size: 14px;
            font-weight: 600;
        }

        .performance-metrics {
            display: grid;
            gap: 12px;
        }

        .performance-item {
            display: flex;
            justify-content: space-between;
            align-items: center;
        }

        .performance-label {
            font-size: 12px;
            color: var(--text-secondary);
        }

        .performance-value {
            font-size: 13px;
            font-weight: 600;
            color: var(--ajung-light);
        }

        /* 토스트 */
        .toast {
            position: fixed;
            top: 100px;
            right: 24px;
            padding: 16px 24px;
            background: linear-gradient(135deg, var(--bg-card), var(--bg-hover));
            border: 1px solid var(--ajung-blue);
            border-radius: 12px;
            box-shadow: 0 10px 30px rgba(30, 111, 255, 0.3);
            display: flex;
            align-items: center;
            gap: 12px;
            animation: slideInRight 0.3s ease-out;
            z-index: 2000;
        }

        @keyframes slideInRight {
            from {
                transform: translateX(400px);
                opacity: 0;
            }
            to {
                transform: translateX(0);
                opacity: 1;
            }
        }

        /* 로딩 */
        .spinner {
            display: inline-block;
            width: 20px;
            height: 20px;
            border: 3px solid rgba(30, 111, 255, 0.2);
            border-top-color: var(--ajung-blue);
            border-radius: 50%;
            animation: spin 1s linear infinite;
        }

        @keyframes spin {
            to { transform: rotate(360deg); }
        }

        /* 빈 상태 */
        .empty-state {
            grid-column: 1 / -1;
            text-align: center;
            padding: 80px 20px;
        }

        .empty-icon {
            font-size: 80px;
            margin-bottom: 24px;
            filter: grayscale(50%);
        }

        .empty-title {
            font-size: 28px;
            font-weight: 700;
            margin-bottom: 12px;
            background: linear-gradient(135deg, var(--text-primary), var(--ajung-light));
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
        }

        .empty-desc {
            color: var(--text-secondary);
            font-size: 16px;
        }

        /* 반응형 */
        @media (max-width: 768px) {
            .chat-grid {
                grid-template-columns: 1fr;
            }
            
            .metrics-grid {
                grid-template-columns: repeat(2, 1fr);
            }
            
            .priority-stats {
                grid-template-columns: repeat(2, 1fr);
            }
            
            .header-stats {
                display: none;
            }
        }
    </style>
</head>
<body>
    <header class="header">
        <div class="header-content">
            <div class="logo-section">
                <div class="logo">아정당</div>
                <h1 class="title">채널톡 모니터 PRO</h1>
            </div>
            
            <div class="header-stats">
                <div class="header-stat">
                    <div class="header-stat-value" id="totalActive">0</div>
                    <div class="header-stat-label">활성 상담</div>
                </div>
                <div class="header-stat">
                    <div class="header-stat-value" id="avgResponseTime">0</div>
                    <div class="header-stat-label">평균 응답(분)</div>
                </div>
                <div class="header-stat">
                    <div class="header-stat-value" id="todayAnswered">0</div>
                    <div class="header-stat-label">오늘 처리</div>
                </div>
            </div>
            
            <div class="sync-indicator">
                <span class="sync-dot"></span>
                <span id="syncStatus">실시간 모니터링</span>
            </div>
        </div>
    </header>

    <div class="container">
        <!-- 메트릭 카드 -->
        <div class="metrics-grid">
            <div class="metric-card">
                <div class="metric-icon">📊</div>
                <div class="metric-label">총 접수</div>
                <div class="metric-value" id="totalReceived">0</div>
                <div class="metric-change positive">
                    <span>↑</span>
                    <span id="receivedChange">0% 증가</span>
                </div>
            </div>
            
            <div class="metric-card">
                <div class="metric-icon">✅</div>
                <div class="metric-label">처리 완료</div>
                <div class="metric-value" id="totalAnswered">0</div>
                <div class="metric-change positive">
                    <span>↑</span>
                    <span id="answeredChange">0% 증가</span>
                </div>
            </div>
            
            <div class="metric-card">
                <div class="metric-icon">⚡</div>
                <div class="metric-label">처리율</div>
                <div class="metric-value" id="answerRate">0%</div>
                <div class="metric-change">
                    <span id="rateChange">안정적</span>
                </div>
            </div>
            
            <div class="metric-card">
                <div class="metric-icon">🎯</div>
                <div class="metric-label">캐시 적중률</div>
                <div class="metric-value" id="cacheHitRate">0%</div>
                <div class="metric-change positive">
                    <span>최적화됨</span>
                </div>
            </div>
        </div>

        <!-- 우선순위 통계 -->
        <div class="priority-stats">
            <div class="priority-stat critical">
                <div class="priority-count" id="criticalCount">0</div>
                <div class="priority-label">긴급 (11분↑)</div>
            </div>
            <div class="priority-stat warning">
                <div class="priority-count" id="warningCount">0</div>
                <div class="priority-label">경고 (8-10분)</div>
            </div>
            <div class="priority-stat caution">
                <div class="priority-count" id="cautionCount">0</div>
                <div class="priority-label">주의 (5-7분)</div>
            </div>
            <div class="priority-stat normal">
                <div class="priority-count" id="normalCount">0</div>
                <div class="priority-label">일반 (2-4분)</div>
            </div>
            <div class="priority-stat new">
                <div class="priority-count" id="newCount">0</div>
                <div class="priority-label">신규 (2분↓)</div>
            </div>
        </div>

        <!-- 컨트롤 바 -->
        <div class="control-bar">
            <div class="control-group">
                <button class="btn" onclick="syncNow()">
                    <span id="syncBtnText">🔄 즉시 동기화</span>
                </button>
                <button class="btn btn-secondary" onclick="toggleAutoRefresh()">
                    <span id="autoRefreshBtn">⏸ 자동 새로고침</span>
                </button>
                <button class="btn btn-secondary" onclick="openAnalytics()">
                    📈 분석 대시보드
                </button>
            </div>
            
            <div class="filter-tabs">
                <button class="filter-tab active" onclick="filterByPriority('all')">전체</button>
                <button class="filter-tab" onclick="filterByPriority('critical')">긴급</button>
                <button class="filter-tab" onclick="filterByPriority('warning')">경고</button>
                <button class="filter-tab" onclick="filterByPriority('caution')">주의</button>
                <button class="filter-tab" onclick="filterByPriority('normal')">일반</button>
                <button class="filter-tab" onclick="filterByPriority('new')">신규</button>
            </div>
        </div>

        <!-- 채팅 그리드 -->
        <div class="chat-grid" id="chatGrid">
            <!-- 동적 생성 -->
        </div>
    </div>

    <!-- 퍼포먼스 모니터 -->
    <div class="performance-panel" id="performancePanel">
        <div class="performance-header">
            <span class="performance-title">🚀 시스템 성능</span>
            <button onclick="togglePerformance()" style="background: none; border: none; color: var(--text-secondary); cursor: pointer;">
                ⚙️
            </button>
        </div>
        <div class="performance-metrics" id="performanceMetrics">
            <div class="performance-item">
                <span class="performance-label">응답 시간</span>
                <span class="performance-value" id="perfResponseTime">0ms</span>
            </div>
            <div class="performance-item">
                <span class="performance-label">API 호출</span>
                <span class="performance-value" id="perfApiCall">0ms</span>
            </div>
            <div class="performance-item">
                <span class="performance-label">WebSocket</span>
                <span class="performance-value" id="perfWebsocket">연결됨</span>
            </div>
            <div class="performance-item">
                <span class="performance-label">메모리 사용</span>
                <span class="performance-value" id="perfMemory">0MB</span>
            </div>
            <div class="performance-item">
                <span class="performance-label">업타임</span>
                <span class="performance-value" id="perfUptime">0시간</span>
            </div>
        </div>
    </div>

    <script>
        // 전역 상태
        let ws = null;
        let chats = [];
        let currentFilter = 'all';
        let autoRefresh = true;
        let stats = {};
        let performance = {};

        // 우선순위 매핑
        const priorityColors = {
            critical: 'var(--critical)',
            warning: 'var(--warning)',
            caution: 'var(--caution)',
            normal: 'var(--normal)',
            new: 'var(--new)'
        };

        // 대기시간 포맷
        function formatWaitTime(minutes) {
            if (minutes < 1) return '방금 전';
            if (minutes < 60) return `${Math.floor(minutes)}분`;
            const hours = Math.floor(minutes / 60);
            const mins = minutes % 60;
            return `${hours}시간 ${mins}분`;
        }

        // 시간 포맷
        function formatDateTime(timestamp) {
            const date = new Date(timestamp);
            const now = new Date();
            const diff = now - date;
            
            if (diff < 60000) return '방금 전';
            if (diff < 3600000) return `${Math.floor(diff / 60000)}분 전`;
            if (diff < 86400000) return `${Math.floor(diff / 3600000)}시간 전`;
            
            return date.toLocaleString('ko-KR');
        }

        // 채팅 카드 렌더링
        function renderChats() {
            const grid = document.getElementById('chatGrid');
            
            // 필터링
            let filteredChats = chats;
            if (currentFilter !== 'all') {
                filteredChats = chats.filter(chat => chat.priority === currentFilter);
            }
            
            if (filteredChats.length === 0) {
                grid.innerHTML = `
                    <div class="empty-state">
                        <div class="empty-icon">✨</div>
                        <h2 class="empty-title">
                            ${currentFilter === 'all' ? '모든 상담이 처리되었습니다' : '해당 우선순위 상담이 없습니다'}
                        </h2>
                        <p class="empty-desc">
                            ${currentFilter === 'all' ? '현재 대기 중인 미답변 상담이 없습니다' : '다른 우선순위 탭을 확인해보세요'}
                        </p>
                    </div>
                `;
            } else {
                grid.innerHTML = filteredChats.map(chat => {
                    const waitBadgeColor = priorityColors[chat.priority];
                    const tags = chat.tags || [];
                    
                    return `
                        <div class="chat-card" onclick="openChat('${chat.id}')">
                            <div class="priority-indicator priority-${chat.priority}"></div>
                            
                            <div class="chat-header">
                                <div class="customer-info">
                                    <div class="customer-name">${chat.customerName || '익명 고객'}</div>
                                    <div class="chat-meta">
                                        ${tags.map(tag => `<span class="meta-tag">${tag}</span>`).join('')}
                                        ${chat.source === 'api' ? '<span class="meta-tag">API</span>' : ''}
                                        ${chat.channelId ? `<span class="meta-tag">#${chat.channelId}</span>` : ''}
                                    </div>
                                </div>
                                <div class="wait-badge" style="background: ${waitBadgeColor}; color: ${chat.priority === 'warning' || chat.priority === 'caution' ? '#000' : '#fff'}">
                                    ⏱ ${formatWaitTime(chat.waitMinutes)}
                                </div>
                            </div>
                            
                            <div class="message-content">
                                ${chat.lastMessage || '메시지 내용이 없습니다'}
                            </div>
                            
                            <div class="chat-footer">
                                <span class="chat-time">
                                    ${formatDateTime(chat.timestamp)}
                                </span>
                                <div class="chat-actions">
                                    <button class="action-btn" onclick="event.stopPropagation(); markAnswered('${chat.id}')">
                                        답변 완료
                                    </button>
                                </div>
                            </div>
                        </div>
                    `;
                }).join('');
            }
            
            updateUI();
        }

        // UI 업데이트
        function updateUI() {
            // 우선순위별 카운트
            const counts = {
                critical: 0,
                warning: 0,
                caution: 0,
                normal: 0,
                new: 0
            };
            
            chats.forEach(chat => {
                if (counts[chat.priority] !== undefined) {
                    counts[chat.priority]++;
                }
            });
            
            document.getElementById('criticalCount').textContent = counts.critical;
            document.getElementById('warningCount').textContent = counts.warning;
            document.getElementById('cautionCount').textContent = counts.caution;
            document.getElementById('normalCount').textContent = counts.normal;
            document.getElementById('newCount').textContent = counts.new;
            
            // 헤더 통계
            document.getElementById('totalActive').textContent = chats.length;
            
            // 메트릭 카드
            if (stats) {
                document.getElementById('totalReceived').textContent = stats.totalReceived || 0;
                document.getElementById('totalAnswered').textContent = stats.totalAnswered || 0;
                document.getElementById('avgResponseTime').textContent = 
                    Math.round(stats.avgResponseTime || 0);
                
                const answerRate = stats.totalReceived > 0 
                    ? Math.round((stats.totalAnswered / stats.totalReceived) * 100)
                    : 0;
                document.getElementById('answerRate').textContent = answerRate + '%';
                
                const cacheHitRate = Math.round((stats.cacheHitRatio || 0) * 100);
                document.getElementById('cacheHitRate').textContent = cacheHitRate + '%';
                
                document.getElementById('todayAnswered').textContent = stats.totalAnswered || 0;
            }
            
            // 퍼포먼스
            if (performance) {
                document.getElementById('perfResponseTime').textContent = 
                    Math.round((performance.response_time_avg || 0) * 1000) + 'ms';
                document.getElementById('perfApiCall').textContent = 
                    Math.round((performance.api_call_avg || 0) * 1000) + 'ms';
                
                const uptime = performance.uptime_seconds || 0;
                const hours = Math.floor(uptime / 3600);
                const minutes = Math.floor((uptime % 3600) / 60);
                document.getElementById('perfUptime').textContent = `${hours}시간 ${minutes}분`;
            }
        }

        // WebSocket 연결
        function connectWebSocket() {
            const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
            const wsUrl = `${protocol}//${window.location.host}/ws`;
            
            ws = new WebSocket(wsUrl);
            
            ws.onopen = () => {
                console.log('WebSocket 연결됨');
                document.getElementById('syncStatus').textContent = '실시간 모니터링';
                document.getElementById('perfWebsocket').textContent = '연결됨';
            };
            
            ws.onmessage = (event) => {
                const data = JSON.parse(event.data);
                
                if (data.type === 'initial') {
                    chats = data.chats;
                    stats = data.stats;
                    performance = data.performance;
                    renderChats();
                } else if (data.type === 'new_chat') {
                    // 중복 체크
                    const exists = chats.find(c => c.id === data.chat.id);
                    if (!exists) {
                        chats.push(data.chat);
                        chats.sort((a, b) => {
                            const priorityOrder = { critical: 0, warning: 1, caution: 2, normal: 3, new: 4 };
                            if (priorityOrder[a.priority] !== priorityOrder[b.priority]) {
                                return priorityOrder[a.priority] - priorityOrder[b.priority];
                            }
                            return b.waitMinutes - a.waitMinutes;
                        });
                        renderChats();
                        showToast('새 상담이 접수되었습니다', '📨');
                        playNotificationSound();
                    }
                    if (data.stats) stats = data.stats;
                } else if (data.type === 'chat_removed') {
                    chats = chats.filter(c => c.id !== data.chatId);
                    renderChats();
                    if (data.stats) stats = data.stats;
                } else if (data.type === 'sync_complete') {
                    showToast(`동기화 완료: ${data.count}개 상담`, '✅');
                }
            };
            
            ws.onerror = (error) => {
                console.error('WebSocket 오류:', error);
                document.getElementById('syncStatus').textContent = '연결 끊김';
                document.getElementById('perfWebsocket').textContent = '오류';
            };
            
            ws.onclose = () => {
                document.getElementById('syncStatus').textContent = '재연결 중...';
                document.getElementById('perfWebsocket').textContent = '재연결 중';
                setTimeout(connectWebSocket, 5000);
            };
        }

        // API 동기화
        async function syncNow() {
            const btn = document.getElementById('syncBtnText');
            btn.innerHTML = '<span class="spinner"></span> 동기화 중...';
            
            try {
                await fetch('/api/sync', { method: 'POST' });
                btn.innerHTML = '🔄 즉시 동기화';
            } catch (error) {
                console.error('동기화 실패:', error);
                btn.innerHTML = '🔄 즉시 동기화';
                showToast('동기화 실패', '❌');
            }
        }

        // 답변 완료
        async function markAnswered(chatId) {
            try {
                await fetch(`/api/chats/${chatId}/answer`, { method: 'POST' });
                chats = chats.filter(c => c.id !== chatId);
                renderChats();
                showToast('답변 완료 처리', '✅');
            } catch (error) {
                console.error('처리 실패:', error);
                showToast('처리 실패', '❌');
            }
        }

        // 채널톡 열기
        function openChat(chatId) {
            window.open(`https://desk.channel.io/#/channels/chats/${chatId}`, '_blank');
        }

        // 필터링
        function filterByPriority(priority) {
            currentFilter = priority;
            
            // 탭 활성화
            document.querySelectorAll('.filter-tab').forEach(tab => {
                tab.classList.remove('active');
            });
            event.target.classList.add('active');
            
            renderChats();
        }

        // 자동 새로고침
        function toggleAutoRefresh() {
            autoRefresh = !autoRefresh;
            const btn = document.getElementById('autoRefreshBtn');
            btn.textContent = autoRefresh ? '⏸ 자동 새로고침' : '▶ 자동 새로고침';
            
            if (autoRefresh) {
                startAutoRefresh();
            } else {
                stopAutoRefresh();
            }
        }

        let refreshInterval;
        function startAutoRefresh() {
            refreshInterval = setInterval(async () => {
                const response = await fetch('/api/chats');
                const data = await response.json();
                chats = data.chats;
                stats = data.stats;
                performance = data.performance;
                renderChats();
            }, 5000);
        }

        function stopAutoRefresh() {
            clearInterval(refreshInterval);
        }

        // 분석 대시보드
        function openAnalytics() {
            window.open('/analytics', '_blank');
        }

        // 퍼포먼스 패널 토글
        function togglePerformance() {
            const panel = document.getElementById('performancePanel');
            const metrics = document.getElementById('performanceMetrics');
            
            if (metrics.style.display === 'none') {
                metrics.style.display = 'grid';
                panel.classList.remove('collapsed');
            } else {
                metrics.style.display = 'none';
                panel.classList.add('collapsed');
            }
        }

        // 토스트
        function showToast(message, icon = '📢') {
            const toast = document.createElement('div');
            toast.className = 'toast';
            toast.innerHTML = `
                <span style="font-size: 20px;">${icon}</span>
                <span>${message}</span>
            `;
            document.body.appendChild(toast);
            
            setTimeout(() => {
                toast.style.animation = 'slideInRight 0.3s ease-out reverse';
                setTimeout(() => toast.remove(), 300);
            }, 3000);
        }

        // 알림음
        function playNotificationSound() {
            const audio = new Audio('data:audio/wav;base64,UklGRnoGAABXQVZFZm10IBAAAAABAAEAQB8AAEAfAAABAAgAZGF0YQoGAACBhYqFbF1fdJivrJBhNjVgodDbq2EcBj+a2/LDciUFLIHO8tiJNwgZaLvt559NEAxQp+PwtmMcBjiR1/LMeSwFJHfH8N2QQAoUXrTp66hVFApGn+DyvmwhBSuBzvLZijYJGmm98OScTgwOUann7blmFgU7k9n1unEiBC13yO/eizEIHWq+8+OWT');
            audio.volume = 0.3;
            audio.play().catch(e => console.log('알림음 재생 실패'));
        }

        // 초기화
        connectWebSocket();
        startAutoRefresh();
        
        // 페이지 포커스 시 새로고침
        document.addEventListener('visibilitychange', () => {
            if (!document.hidden && autoRefresh) {
                fetch('/api/chats')
                    .then(res => res.json())
                    .then(data => {
                        chats = data.chats;
                        stats = data.stats;
                        performance = data.performance;
                        renderChats();
                    });
            }
        });
        
        // 키보드 단축키
        document.addEventListener('keydown', (e) => {
            if (e.key === 'r' && e.ctrlKey) {
                e.preventDefault();
                syncNow();
            }
        });
    </script>
</body>
</html>
"""

# ===== 앱 생성 =====
async def create_app():
    """Enterprise 애플리케이션 생성"""
    # uvloop 사용 (성능 향상)
    asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())
    
    logger.info("🚀 Enterprise 채널톡 모니터링 시스템 시작")
    logger.info(f"⚙️ CPU: {CPU_COUNT}, Workers: {WORKER_COUNT}, Redis Pool: {REDIS_POOL_SIZE}")
    
    monitor = EnterpriseChannelTalkMonitor()
    await monitor.setup()
    
    app = web.Application(client_max_size=10*1024*1024)  # 10MB 제한
    app['monitor'] = monitor
    
    # 라우트 설정
    app.router.add_post('/webhook', monitor.handle_webhook)
    app.router.add_get('/api/chats', monitor.get_chats)
    app.router.add_post('/api/chats/{chat_id}/answer', monitor.mark_answered)
    app.router.add_post('/api/sync', monitor.force_sync)
    app.router.add_get('/api/analytics', monitor.get_analytics)
    app.router.add_get('/ws', monitor.handle_websocket)
    app.router.add_get('/health', monitor.health_check)
    app.router.add_get('/', monitor.serve_dashboard)
    
    # CORS 설정
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
        logger.info("🏢 아정당 채널톡 모니터링 시스템 - ENTERPRISE PRO")
        logger.info(f"📌 대시보드: http://localhost:{PORT}")
        logger.info(f"🔥 고성능 모드: {CPU_COUNT} CPU, {WORKER_COUNT} Workers")
        logger.info(f"💾 Redis: {REDIS_POOL_SIZE} 연결 풀")
        logger.info("=" * 60)
    
    async def on_cleanup(app):
        await monitor.cleanup()
        logger.info("👋 시스템 안전 종료")
    
    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    
    return app

# ===== 메인 실행 =====
if __name__ == '__main__':
    # 멀티프로세싱 지원
    import multiprocessing
    multiprocessing.set_start_method('spawn', force=True)
    
    # 이벤트 루프 생성 및 실행
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    
    app = loop.run_until_complete(create_app())
    
    # 서버 실행 (고성능 설정)
    web.run_app(
        app, 
        host='0.0.0.0', 
        port=PORT,
        access_log_format='%a %t "%r" %s %b "%{Referer}i" "%{User-Agent}i" %Tf'
    )
