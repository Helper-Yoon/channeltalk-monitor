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

# Channel API 설정 (매니저 정보 조회용)
# Channel Desk > 설정 > 보안 및 개발자 > Open API에서 키 발급
# https://desk.channel.io/#/channels/197228/settings/security
CHANNEL_API_KEY = os.getenv('CHANNEL_API_KEY', '')  
CHANNEL_API_SECRET = os.getenv('CHANNEL_API_SECRET', '')
CHANNEL_API_BASE_URL = 'https://api.channel.io'

# API 키가 없으면 경고 메시지와 함께 설정 방법 안내
if not CHANNEL_API_KEY or not CHANNEL_API_SECRET:
    print("=" * 80)
    print("⚠️  Channel API 키가 설정되지 않았습니다! 담당자 정보를 가져올 수 없습니다.")
    print("=" * 80)
    print("\n📌 API 키 설정 방법:\n")
    print("1. Channel Desk 접속: https://desk.channel.io")
    print("2. 설정 > 보안 및 개발자 > Open API 메뉴 이동")
    print("3. 'API 키 생성' 버튼 클릭")
    print("4. 생성된 Access Key와 Access Secret 복사")
    print("5. 환경변수 설정:")
    print("   export CHANNEL_API_KEY='your_access_key_here'")
    print("   export CHANNEL_API_SECRET='your_access_secret_here'")
    print("\n또는 코드에 직접 입력:")
    print("   CHANNEL_API_KEY = 'your_access_key_here'")
    print("   CHANNEL_API_SECRET = 'your_access_secret_here'")
    print("\n=" * 80)

# ===== 로깅 설정 =====
# 담당자 정보 디버깅이 필요하면 level=logging.DEBUG로 변경
logging.basicConfig(
    level=logging.INFO,  # DEBUG로 변경하면 더 자세한 로그 확인 가능
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger('ChannelTalk')

# ===== 상수 정의 =====
CACHE_TTL = 43200  # 12시간
PING_INTERVAL = 30  # WebSocket ping 간격
SYNC_INTERVAL = 30  # 30초마다 시간 업데이트
MAX_RECONNECT_ATTEMPTS = 5
RECONNECT_DELAY = 5

# 팀 설정
TEAMS = {
    'SNS 1팀': ['이종민', '정주연', '이혜영', '김국현', '정다혜', '조시현', '김시윤'],
    'SNS 2팀': ['윤도우리', '신혜서', '김상아', '박은진', '오민환', '서정국'],
    'SNS 3팀': ['김진후', '김시진', '권재현', '김지원', '최호익', '김진협', '박해영'],
    'SNS 4팀': ['이민주', '전지윤', '전미란', '김채영', '김영진', '공현준'],
    '의정부 SNS팀': ['차정환', '최수능', '구본영', '서민국', '오민경', '김범주', '동수진', '성일훈']
}

# 팀원별 팀 매핑 (빠른 조회용)
MEMBER_TO_TEAM = {}
for team, members in TEAMS.items():
    for member in members:
        MEMBER_TO_TEAM[member] = team

class ChannelTalkMonitor:
    """고성능 Redis 기반 채널톡 모니터링 시스템"""
    
    def __init__(self):
        self.redis: Optional[aioredis.Redis] = None
        self.redis_pool = None
        self.websockets = weakref.WeakSet()
        self.chat_cache: Dict[str, dict] = {}
        self.chat_messages: Dict[str, Set[str]] = {}  # 채팅별 메시지 해시 저장
        self.manager_cache: Dict[str, dict] = {}  # 매니저 ID-정보 매핑
        self.last_sync = 0
        self.stats = defaultdict(int)
        self._running = False
        self._sync_task = None
        self._cleanup_task = None
        self._time_update_task = None
        self._api_enrich_task = None
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
            
            # 매니저 정보 로드 (API 사용 가능한 경우)
            await self.load_managers()
            
            # 초기 데이터 로드 및 정리
            await self._initial_load()
            
            # 백그라운드 태스크 시작
            self._running = True
            self._sync_task = asyncio.create_task(self._periodic_sync())
            self._cleanup_task = asyncio.create_task(self._periodic_cleanup())
            self._time_update_task = asyncio.create_task(self._periodic_time_update())
            self._api_enrich_task = asyncio.create_task(self._periodic_api_enrich())
            
        except Exception as e:
            logger.error(f"❌ Redis 연결 실패: {e}")
            raise
    
    async def get_userchat_from_api(self, chat_id: str) -> dict:
        """Channel API를 통해 UserChat 정보 조회"""
        try:
            if not CHANNEL_API_KEY or not CHANNEL_API_SECRET:
                return None
            
            headers = {
                'x-access-key': CHANNEL_API_KEY,
                'x-access-secret': CHANNEL_API_SECRET,
                'Content-Type': 'application/json'
            }
            
            # UserChat 정보 조회 - 여러 API 버전 시도
            async with aiohttp.ClientSession() as session:
                # v5 API 먼저 시도
                for api_version in ['v5', 'v4']:
                    url = f'{CHANNEL_API_BASE_URL}/open/{api_version}/user-chats/{chat_id}'
                    
                    async with session.get(url, headers=headers) as response:
                        if response.status == 200:
                            data = await response.json()
                            user_chat = data.get('userChat', {})
                            
                            logger.info(f"✅ API {api_version}로 UserChat 조회 성공: {chat_id}")
                            
                            # assigneeId 확인
                            assignee_id = user_chat.get('assigneeId')
                            manager_ids = user_chat.get('managerIds', [])
                            
                            logger.info(f"📋 UserChat 정보 - assigneeId: {assignee_id}, managerIds: {manager_ids}")
                            
                            return user_chat
                        elif response.status == 404:
                            logger.debug(f"UserChat not found in {api_version}: {chat_id}")
                            continue
                        else:
                            logger.warning(f"UserChat API {api_version} 조회 실패: {chat_id} - {response.status}")
                
                return None
                        
        except Exception as e:
            logger.error(f"❌ UserChat API 조회 오류 [{chat_id}]: {e}")
            return None
    
    async def get_manager_from_api(self, manager_id: str) -> dict:
        """Channel API를 통해 Manager 정보 조회"""
        try:
            # 캐시 확인
            if manager_id in self.manager_cache:
                return self.manager_cache[manager_id]
            
            if not CHANNEL_API_KEY or not CHANNEL_API_SECRET:
                return None
            
            headers = {
                'x-access-key': CHANNEL_API_KEY,
                'x-access-secret': CHANNEL_API_SECRET,
                'Content-Type': 'application/json'
            }
            
            # 개별 Manager 정보 조회 (캐시에 없는 경우)
            async with aiohttp.ClientSession() as session:
                # 먼저 전체 매니저 목록 재조회 시도
                async with session.get(
                    f'{CHANNEL_API_BASE_URL}/open/v4/managers?limit=100',
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=10)
                ) as response:
                    if response.status == 200:
                        data = await response.json()
                        managers = data.get('managers', [])
                        
                        # 전체 캐시 업데이트
                        for manager in managers:
                            mid = manager.get('id')
                            if mid:
                                manager_info = {
                                    'name': manager.get('name') or manager.get('displayName'),
                                    'email': manager.get('email'),
                                    'username': manager.get('username')
                                }
                                self.manager_cache[mid] = manager_info
                                
                                if mid == manager_id:
                                    logger.debug(f"✅ Manager 발견: {manager_info['name']}")
                                    return manager_info
                        
                        logger.warning(f"⚠️ Manager ID {manager_id} not found in list")
                        return None
                    elif response.status == 401:
                        logger.error("❌ API 인증 실패 - API 키를 확인하세요")
                        return None
                    else:
                        logger.warning(f"Manager API 조회 실패: {response.status}")
                        return None
                        
        except asyncio.TimeoutError:
            logger.error(f"⏱️ Manager API 조회 시간 초과 [{manager_id}]")
            return None
        except Exception as e:
            logger.error(f"❌ Manager API 조회 오류 [{manager_id}]: {e}")
            return None
    
    async def enrich_chat_with_api_data(self, chat_data: dict) -> dict:
        """API를 통해 채팅 데이터에 담당자 정보 추가"""
        try:
            chat_id = chat_data.get('id')
            
            # 이미 담당자 정보가 있으면 스킵
            if chat_data.get('assignee') and chat_data['assignee'] != '미배정':
                return chat_data
            
            if not CHANNEL_API_KEY or not CHANNEL_API_SECRET:
                return chat_data
            
            # UserChat 정보 조회
            user_chat = await self.get_userchat_from_api(chat_id)
            if not user_chat:
                logger.debug(f"UserChat 조회 실패: {chat_id}")
                return chat_data
            
            # assigneeId 또는 managerIds 확인
            assignee_id = user_chat.get('assigneeId')
            manager_ids = user_chat.get('managerIds', [])
            team_id = user_chat.get('teamId')
            
            logger.info(f"📋 UserChat API 응답 - assigneeId: {assignee_id}, managerIds: {manager_ids}, teamId: {team_id}")
            
            # assigneeId가 있으면 우선 사용
            if assignee_id:
                manager_info = await self.get_manager_from_api(assignee_id)
                if manager_info and manager_info.get('name'):
                    chat_data['assignee'] = manager_info['name']
                    chat_data['assigneeId'] = assignee_id
                    
                    # 팀 정보 매핑
                    if manager_info['name'] in MEMBER_TO_TEAM:
                        chat_data['team'] = MEMBER_TO_TEAM[manager_info['name']]
                    
                    logger.info(f"✅ API로 담당자 정보 업데이트: {chat_id} -> {manager_info['name']}")
            
            # assigneeId가 없으면 첫 번째 manager 사용
            elif manager_ids and len(manager_ids) > 0:
                for manager_id in manager_ids:
                    manager_info = await self.get_manager_from_api(manager_id)
                    if manager_info and manager_info.get('name'):
                        chat_data['assignee'] = manager_info['name']
                        chat_data['assigneeId'] = manager_id
                        
                        # 팀 정보 매핑
                        if manager_info['name'] in MEMBER_TO_TEAM:
                            chat_data['team'] = MEMBER_TO_TEAM[manager_info['name']]
                        
                        logger.info(f"✅ API로 매니저 정보 업데이트: {chat_id} -> {manager_info['name']}")
                        break
            
            # 여전히 담당자 정보가 없으면 기본값 설정
            if not chat_data.get('assignee'):
                chat_data['assignee'] = '미배정'
            if not chat_data.get('team'):
                chat_data['team'] = '미배정'
            
            return chat_data
            
        except Exception as e:
            logger.error(f"❌ API 데이터 보강 실패 [{chat_data.get('id')}]: {e}")
            return chat_data
    
    async def load_managers(self):
        """채널톡 API를 통해 매니저 정보 로드"""
        try:
            if not CHANNEL_API_KEY or not CHANNEL_API_SECRET:
                logger.warning("⚠️ Channel API 키가 설정되지 않음 - 매니저 정보 캐싱 건너뜀")
                logger.warning("   모든 상담이 '미배정'으로 표시됩니다!")
                return
            
            # API 호출하여 매니저 목록 가져오기
            headers = {
                'x-access-key': CHANNEL_API_KEY,
                'x-access-secret': CHANNEL_API_SECRET,
                'Content-Type': 'application/json'
            }
            
            async with aiohttp.ClientSession() as session:
                # 전체 매니저 목록 조회 (최대 100명)
                async with session.get(
                    f'{CHANNEL_API_BASE_URL}/open/v4/managers?limit=100',
                    headers=headers
                ) as response:
                    if response.status == 200:
                        data = await response.json()
                        managers = data.get('managers', [])
                        
                        # 매니저 정보 캐싱
                        for manager in managers:
                            manager_id = manager.get('id')
                            if manager_id:
                                manager_name = manager.get('name') or manager.get('displayName')
                                self.manager_cache[manager_id] = {
                                    'name': manager_name,
                                    'email': manager.get('email'),
                                    'username': manager.get('username')
                                }
                                
                                # 팀 정보도 미리 확인
                                if manager_name in MEMBER_TO_TEAM:
                                    logger.debug(f"매니저 {manager_name} - 팀: {MEMBER_TO_TEAM[manager_name]}")
                        
                        logger.info(f"✅ {len(self.manager_cache)}명의 매니저 정보 로드 완료")
                        
                        # 로드된 매니저 목록 출력 (디버깅용)
                        logger.debug(f"로드된 매니저: {list(self.manager_cache.values())[:5]}...")
                    else:
                        logger.error(f"❌ 매니저 API 호출 실패: {response.status}")
                        text = await response.text()
                        logger.error(f"응답: {text}")
                        
        except Exception as e:
            logger.error(f"❌ 매니저 정보 로드 실패: {e}")
            logger.error(f"API Key 설정 확인 필요: CHANNEL_API_KEY={bool(CHANNEL_API_KEY)}, CHANNEL_API_SECRET={bool(CHANNEL_API_SECRET)}")
    
    async def cleanup(self):
        """종료시 정리"""
        self._running = False
        
        for task in [self._sync_task, self._cleanup_task, self._time_update_task, self._api_enrich_task]:
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
            api_enriched_count = 0
            
            current_time = datetime.now(timezone.utc)
            
            # API 사용 가능 여부 확인
            api_available = bool(CHANNEL_API_KEY and CHANNEL_API_SECRET)
            
            for chat_id in existing_ids:
                chat_data = await self.redis.get(f"chat:{chat_id}")
                if chat_data:
                    try:
                        data = json.loads(chat_data)
                        # 오래된 상담 체크 (12시간 이상)
                        timestamp = data.get('timestamp')
                        if timestamp:
                            # ISO 형식 파싱
                            if isinstance(timestamp, str):
                                created = datetime.fromisoformat(timestamp.replace('Z', '+00:00'))
                            else:
                                created = datetime.fromtimestamp(timestamp / 1000, tz=timezone.utc)
                            
                            age_hours = (current_time - created).total_seconds() / 3600
                            
                            if age_hours > 12:
                                # 오래된 상담 제거
                                await self.redis.srem('unanswered_chats', chat_id)
                                await self.redis.delete(f"chat:{chat_id}")
                                removed_count += 1
                                logger.info(f"🧹 오래된 상담 제거: {chat_id} ({age_hours:.1f}시간)")
                            else:
                                # API를 통해 담당자 정보 보강 (API 사용 가능시)
                                if api_available and (not data.get('assignee') or data.get('assignee') == '미배정'):
                                    logger.debug(f"초기 로드 - API 보강 시도: {chat_id}")
                                    enriched_data = await self.enrich_chat_with_api_data(data)
                                    if enriched_data.get('assignee') and enriched_data['assignee'] != '미배정':
                                        # Redis에 업데이트된 데이터 저장
                                        await self.redis.setex(f"chat:{chat_id}", CACHE_TTL, json.dumps(enriched_data))
                                        data = enriched_data
                                        api_enriched_count += 1
                                    
                                    # API 호출 제한 방지
                                    await asyncio.sleep(0.2)
                                
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
            
            logger.info(f"📥 초기 로드 완료:")
            logger.info(f"   - 유효 상담: {valid_count}개")
            logger.info(f"   - 제거된 상담: {removed_count}개")
            if api_available:
                logger.info(f"   - API로 보강: {api_enriched_count}개")
            else:
                logger.warning(f"   - API 키 없음: 담당자 정보 보강 불가")
            
            # 통계 초기화
            await self.redis.hset('stats:session', mapping={
                'start_time': datetime.now(timezone.utc).isoformat(),
                'initial_count': str(valid_count),
                'removed_old': str(removed_count),
                'api_enriched': str(api_enriched_count),
                'api_available': str(api_available)
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
    
    async def _periodic_time_update(self):
        """주기적 대기시간 업데이트"""
        while self._running:
            try:
                await asyncio.sleep(30)  # 30초마다
                # WebSocket으로 시간 업데이트 브로드캐스트
                await self.broadcast({
                    'type': 'time_update',
                    'timestamp': datetime.now(timezone.utc).isoformat()
                })
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"시간 업데이트 오류: {e}")
    
    async def _periodic_api_enrich(self):
        """주기적으로 API를 통해 담당자 정보 업데이트"""
        while self._running:
            try:
                # 처음 시작시 바로 실행
                if not hasattr(self, '_first_enrich_done'):
                    self._first_enrich_done = True
                    await asyncio.sleep(5)  # 서버 시작 후 5초 대기
                else:
                    await asyncio.sleep(120)  # 2분마다 실행 (5분 -> 2분으로 단축)
                
                if not CHANNEL_API_KEY or not CHANNEL_API_SECRET:
                    logger.warning("⚠️ API 키 없음 - 담당자 정보 업데이트 스킵")
                    continue
                
                enriched_count = 0
                failed_count = 0
                
                for chat_id, chat_data in list(self.chat_cache.items()):
                    # 담당자 정보가 없는 상담만 업데이트
                    if not chat_data.get('assignee') or chat_data.get('assignee') == '미배정':
                        enriched_data = await self.enrich_chat_with_api_data(chat_data)
                        
                        if enriched_data.get('assignee') and enriched_data['assignee'] != '미배정':
                            # 캐시와 Redis 업데이트
                            self.chat_cache[chat_id] = enriched_data
                            await self.redis.setex(f"chat:{chat_id}", CACHE_TTL, json.dumps(enriched_data))
                            enriched_count += 1
                            logger.info(f"✅ 담당자 정보 업데이트: {chat_id} -> {enriched_data['assignee']}")
                        else:
                            failed_count += 1
                        
                        # API 호출 제한 방지 (초당 2개)
                        await asyncio.sleep(0.5)
                
                if enriched_count > 0 or failed_count > 0:
                    logger.info(f"🔄 API 업데이트 완료: 성공 {enriched_count}개, 실패 {failed_count}개")
                    
                    if enriched_count > 0:
                        # WebSocket으로 업데이트 알림
                        await self.broadcast({
                            'type': 'data_enriched',
                            'count': enriched_count,
                            'timestamp': datetime.now(timezone.utc).isoformat()
                        })
                    
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"API 보강 작업 오류: {e}")
                await asyncio.sleep(60)  # 오류시 1분 대기
    
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
                            if isinstance(timestamp, str):
                                created = datetime.fromisoformat(timestamp.replace('Z', '+00:00'))
                            else:
                                created = datetime.fromtimestamp(timestamp / 1000, tz=timezone.utc)
                            
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
    
    def extract_assignee_info(self, data: dict) -> tuple:
        """웹훅 데이터에서 담당자 정보 추출 (개선된 버전)"""
        assignee_name = None
        assignee_id = None
        assignee_team = None
        
        # 1. entity에서 assigneeId 확인
        entity = data.get('entity', {})
        assignee_id = entity.get('assigneeId')
        
        # 2. refers에서 담당자 정보 확인 (여러 경로 시도)
        refers = data.get('refers', {})
        
        # 2-1. refers.assignee 확인
        if 'assignee' in refers and refers['assignee']:
            assignee = refers['assignee']
            assignee_name = (
                assignee.get('name') or 
                assignee.get('displayName') or
                assignee.get('username')
            )
            if not assignee_id:
                assignee_id = assignee.get('id')
        
        # 2-2. refers.userChat.assignee 확인
        user_chat = refers.get('userChat', {})
        if not assignee_name and 'assignee' in user_chat and user_chat['assignee']:
            assignee = user_chat['assignee']
            assignee_name = (
                assignee.get('name') or 
                assignee.get('displayName') or
                assignee.get('username')
            )
            if not assignee_id:
                assignee_id = assignee.get('id')
        
        # 2-3. refers.managers 배열에서 assigneeId로 매칭
        if not assignee_name and assignee_id:
            managers = refers.get('managers', [])
            for manager in managers:
                if manager.get('id') == assignee_id:
                    assignee_name = (
                        manager.get('name') or 
                        manager.get('displayName') or
                        manager.get('username')
                    )
                    break
        
        # 3. 캐시된 매니저 정보에서 조회
        if not assignee_name and assignee_id and assignee_id in self.manager_cache:
            assignee_name = self.manager_cache[assignee_id].get('name')
        
        # 4. 팀 정보 매핑
        if assignee_name and assignee_name in MEMBER_TO_TEAM:
            assignee_team = MEMBER_TO_TEAM[assignee_name]
        
        return assignee_name, assignee_id, assignee_team
    
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
            # 담당자 정보가 없으면 기본값 설정
            if not chat_data.get('assignee'):
                chat_data['assignee'] = '미배정'
            if not chat_data.get('team'):
                chat_data['team'] = '미배정'
            
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
            
            logger.info(f"✅ 저장: {chat_id} - {chat_data.get('customerName', '익명')} - 담당: {chat_data.get('assignee', '없음')} ({chat_data.get('team', '미배정')})")
            
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
                
                # 답변자 랭킹 업데이트 조건:
                # 1. Bot이 아님
                # 2. manager_name이 존재함
                # 3. assignee와 manager_name이 다름 (담당자가 아닌 사람이 답변)
                if manager_name and manager_name.lower() != 'bot':
                    # assignee가 없거나, manager가 assignee와 다른 경우
                    if not assignee or (assignee and manager_name != assignee):
                        today = datetime.now().strftime('%Y-%m-%d')
                        await pipe.hincrby('ranking:daily', f"{today}:{manager_name}", 1)
                        await pipe.hincrby('ranking:total', manager_name, 1)
                        logger.info(f"📊 랭킹 업데이트: {manager_name} (담당자: {assignee or '없음'}) - 카운트!")
                    else:
                        logger.info(f"📊 랭킹 스킵: {manager_name}은 담당자입니다.")
            
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
                    logger.info(f"✅ 제거: {chat_id} (답변자: {manager_name}, 담당자: {assignee})")
                
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
                    timestamp = chat.get('timestamp')
                    if timestamp:
                        # 문자열 또는 숫자 형식 처리
                        if isinstance(timestamp, str):
                            # ISO 형식 문자열
                            created = datetime.fromisoformat(timestamp.replace('Z', '+00:00'))
                        elif isinstance(timestamp, (int, float)):
                            # Unix timestamp (밀리초)
                            created = datetime.fromtimestamp(timestamp / 1000, tz=timezone.utc)
                        else:
                            created = datetime.now(timezone.utc)
                    else:
                        created = datetime.now(timezone.utc)
                    
                    wait_seconds = (current_time - created).total_seconds()
                    
                    # 12시간 이상된 상담 필터링
                    if wait_seconds > 43200:
                        continue
                    
                    chat['waitMinutes'] = max(0, int(wait_seconds / 60))
                    chat['waitSeconds'] = max(0, int(wait_seconds))
                    
                    # 담당자 정보가 없으면 기본값 설정
                    if not chat.get('assignee'):
                        chat['assignee'] = '미배정'
                    if not chat.get('team'):
                        chat['team'] = '미배정'
                    
                    valid_chats.append(chat)
                except Exception as e:
                    logger.error(f"채팅 시간 계산 오류: {e}, chat: {chat}")
                    chat['waitMinutes'] = 0
                    chat['waitSeconds'] = 0
                    chat['assignee'] = chat.get('assignee', '미배정')
                    chat['team'] = chat.get('team', '미배정')
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
            
            # 디버깅용 로그 (담당자 정보가 안 나올 때만 사용)
            # logger.debug(f"웹훅 수신: {json.dumps(data, ensure_ascii=False, indent=2)}")
            
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
            
            logger.info(f"📨 메시지 수신: chat_id={chat_id}, person_type={person_type}")
            
            # 전체 웹훅 데이터 디버깅 (담당자 정보 찾기)
            logger.debug(f"🔍 웹훅 전체 데이터: {json.dumps(data, ensure_ascii=False, indent=2)}")
            
            if person_type == 'user':
                # 고객 메시지
                user_info = refers.get('user', {})
                user_chat = refers.get('userChat', {})
                
                # 담당자 정보 추출 - 모든 가능한 위치 확인
                assignee_name = None
                assignee_id = None
                assignee_team = None
                
                # 1. userChat의 직접 필드들 확인
                if user_chat:
                    assignee_id = user_chat.get('assigneeId')
                    manager_ids = user_chat.get('managerIds', [])
                    
                    logger.debug(f"🔍 userChat.assigneeId: {assignee_id}")
                    logger.debug(f"🔍 userChat.managerIds: {manager_ids}")
                    
                    # assignee 객체가 있으면
                    if 'assignee' in user_chat and user_chat['assignee']:
                        assignee = user_chat['assignee']
                        assignee_name = assignee.get('name') or assignee.get('displayName')
                        logger.debug(f"🔍 userChat.assignee 발견: {assignee}")
                
                # 2. entity의 assigneeId 확인
                if not assignee_id:
                    assignee_id = entity.get('assigneeId')
                    if assignee_id:
                        logger.debug(f"🔍 entity.assigneeId: {assignee_id}")
                
                # 3. refers의 다른 위치들 확인
                if 'assignee' in refers and refers['assignee']:
                    assignee = refers['assignee']
                    if not assignee_name:
                        assignee_name = assignee.get('name') or assignee.get('displayName')
                    if not assignee_id:
                        assignee_id = assignee.get('id')
                    logger.debug(f"🔍 refers.assignee 발견: {assignee}")
                
                # 4. 매니저 목록에서 assigneeId로 매칭
                if assignee_id and not assignee_name:
                    managers = refers.get('managers', [])
                    for manager in managers:
                        if manager.get('id') == assignee_id:
                            assignee_name = manager.get('name') or manager.get('displayName')
                            logger.debug(f"🔍 매니저 목록에서 매칭: {manager}")
                            break
                
                # 5. managerIds를 사용해서 첫 번째 매니저 찾기
                if not assignee_name and user_chat and user_chat.get('managerIds'):
                    manager_ids = user_chat['managerIds']
                    if manager_ids and len(manager_ids) > 0:
                        first_manager_id = manager_ids[0]
                        # managers 목록에서 찾기
                        managers = refers.get('managers', [])
                        for manager in managers:
                            if manager.get('id') == first_manager_id:
                                assignee_name = manager.get('name') or manager.get('displayName')
                                assignee_id = first_manager_id
                                logger.debug(f"🔍 첫 번째 매니저로 설정: {manager}")
                                break
                
                # 6. 캐시된 매니저 정보에서 조회
                if not assignee_name and assignee_id and assignee_id in self.manager_cache:
                    assignee_name = self.manager_cache[assignee_id].get('name')
                    logger.debug(f"🔍 캐시에서 매니저 정보 발견: {assignee_name}")
                
                # 7. 팀 정보 매핑
                if assignee_name and assignee_name in MEMBER_TO_TEAM:
                    assignee_team = MEMBER_TO_TEAM[assignee_name]
                
                # API로 담당자 정보 즉시 조회 (웹훅에 정보가 없는 경우)
                if not assignee_name and CHANNEL_API_KEY:
                    logger.info(f"📡 API로 담당자 정보 조회 시도: {chat_id}")
                    user_chat_api = await self.get_userchat_from_api(chat_id)
                    if user_chat_api:
                        assignee_id = user_chat_api.get('assigneeId')
                        if assignee_id:
                            manager_info = await self.get_manager_from_api(assignee_id)
                            if manager_info:
                                assignee_name = manager_info.get('name')
                                if assignee_name in MEMBER_TO_TEAM:
                                    assignee_team = MEMBER_TO_TEAM[assignee_name]
                                logger.info(f"✅ API로 담당자 정보 획득: {assignee_name} ({assignee_team})")
                
                logger.info(f"📌 최종 담당자 정보: {assignee_name} ({assignee_team}) [ID: {assignee_id}]")
                
                # timestamp 처리 - createdAt 우선 사용
                timestamp = entity.get('createdAt')
                if not timestamp:
                    timestamp = datetime.now(timezone.utc).isoformat()
                else:
                    # 숫자 형식이면 ISO 형식으로 변환
                    if isinstance(timestamp, (int, float)):
                        timestamp = datetime.fromtimestamp(timestamp / 1000, tz=timezone.utc).isoformat()
                
                chat_data = {
                    'id': str(chat_id),
                    'customerName': (
                        user_info.get('name') or 
                        user_chat.get('name') or 
                        user_info.get('profile', {}).get('name') or 
                        '익명'
                    ),
                    'lastMessage': entity.get('plainText', '') or '(메시지 없음)',
                    'timestamp': timestamp,
                    'channel': refers.get('channel', {}).get('name', ''),
                    'tags': user_chat.get('tags', []) if user_chat else [],
                    'assignee': assignee_name,
                    'assigneeId': assignee_id,
                    'team': assignee_team
                }
                
                await self.save_chat(chat_data)
                
            elif person_type in ['manager', 'bot']:
                # 답변시 제거
                manager_info = refers.get('manager', {})
                manager_name = None
                
                if manager_info:
                    manager_name = (
                        manager_info.get('name') or 
                        manager_info.get('displayName') or
                        manager_info.get('username')
                    )
                
                if not manager_name:
                    manager_name = 'Bot' if person_type == 'bot' else 'Unknown'
                
                # 현재 채팅의 담당자 정보 가져오기
                assignee_name = None
                if str(chat_id) in self.chat_cache:
                    assignee_name = self.chat_cache[str(chat_id)].get('assignee')
                
                logger.info(f"💬 답변 처리: manager={manager_name}, assignee={assignee_name}")
                
                # 담당자가 아닌 경우만 랭킹 업데이트
                await self.remove_chat(str(chat_id), manager_name, assignee_name)
                
        except Exception as e:
            logger.error(f"메시지 처리 오류: {e}")
            logger.error(f"데이터: {json.dumps(data, ensure_ascii=False, indent=2)}")
    
    async def process_user_chat(self, data: dict):
        """상담 상태 처리"""
        try:
            entity = data.get('entity', {})
            chat_id = entity.get('id')
            state = entity.get('state')
            
            logger.info(f"📋 UserChat 이벤트: chat_id={chat_id}, state={state}")
            
            # userChat 이벤트에서 담당자 정보 추출
            if chat_id:
                assignee_name = None
                assignee_id = None
                assignee_team = None
                
                # entity에서 직접 assigneeId 확인
                assignee_id = entity.get('assigneeId')
                if assignee_id:
                    logger.debug(f"🔍 UserChat assigneeId: {assignee_id}")
                
                # managerIds 확인
                manager_ids = entity.get('managerIds', [])
                if manager_ids:
                    logger.debug(f"🔍 UserChat managerIds: {manager_ids}")
                    # 첫 번째 매니저를 담당자로 설정
                    if not assignee_id and len(manager_ids) > 0:
                        assignee_id = manager_ids[0]
                
                # refers에서 매니저 정보 가져오기
                refers = data.get('refers', {})
                if assignee_id:
                    managers = refers.get('managers', [])
                    for manager in managers:
                        if manager.get('id') == assignee_id:
                            assignee_name = manager.get('name') or manager.get('displayName')
                            logger.debug(f"🔍 UserChat에서 매니저 발견: {assignee_name}")
                            break
                
                # 캐시에서 조회
                if not assignee_name and assignee_id and assignee_id in self.manager_cache:
                    assignee_name = self.manager_cache[assignee_id].get('name')
                
                # 팀 매핑
                if assignee_name and assignee_name in MEMBER_TO_TEAM:
                    assignee_team = MEMBER_TO_TEAM[assignee_name]
                
                if state == 'opened':
                    # 기존 캐시에 있는 상담이면 담당자 정보 업데이트
                    if str(chat_id) in self.chat_cache:
                        if assignee_name:
                            self.chat_cache[str(chat_id)]['assignee'] = assignee_name
                            self.chat_cache[str(chat_id)]['assigneeId'] = assignee_id
                            self.chat_cache[str(chat_id)]['team'] = assignee_team or '미배정'
                            logger.info(f"📝 담당자 업데이트: {chat_id} -> {assignee_name} ({assignee_team})")
                        else:
                            logger.warning(f"⚠️ UserChat에서 담당자 정보 없음: {chat_id}")
                
                elif state in ['closed', 'resolved', 'snoozed']:
                    await self.remove_chat(str(chat_id))
                
        except Exception as e:
            logger.error(f"상태 처리 오류: {e}")
            logger.error(f"UserChat 데이터: {json.dumps(data, ensure_ascii=False, indent=2)}")
    
    async def get_chats(self, request):
        """API: 채팅 목록"""
        chats = await self.get_all_chats()
        rankings = await self.get_rankings()
        
        # 팀별 필터링 (쿼리 파라미터)
        team_filter = request.query.get('team')
        if team_filter and team_filter != 'all':
            # 해당 팀 구성원들의 담당 상담만 필터링
            if team_filter in TEAMS:
                team_members = TEAMS[team_filter]
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
            'cached_managers': len(self.manager_cache),
            'uptime': int(time.time() - self.stats.get('start_time', time.time())),
            'timestamp': datetime.now(timezone.utc).isoformat()
        })
    
    async def serve_dashboard(self, request):
        """대시보드 HTML 제공"""
        return web.Response(text=DASHBOARD_HTML, content_type='text/html')

# ===== 다크모드 대시보드 HTML (컬럼 순서 변경) =====
DASHBOARD_HTML = """
<!DOCTYPE html>
<html lang="ko">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>채널톡 실시간 모니터링</title>
    <style>
        :root {
            /* 다크모드 색상 */
            --bg-primary: #0F0F0F;
            --bg-secondary: #1A1A1A;
            --bg-card: #252525;
            --bg-hover: #2F2F2F;
            
            /* 텍스트 */
            --text-primary: #FFFFFF;
            --text-secondary: #B0B0B0;
            --text-dim: #808080;
            
            /* 상태 색상 */
            --critical: #FF4444;
            --warning: #FF9F1C;
            --caution: #FFD60A;
            --normal: #4D7FFF;
            --new: #00D68F;
            
            /* 기타 */
            --border: #333333;
            --ajd-blue: #0066CC;
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
            background: var(--bg-card);
            color: var(--text-primary);
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
        }

        .refresh-btn:hover {
            opacity: 0.9;
        }

        /* 통계 바 */
        .stats-bar {
            display: flex;
            gap: 8px;
            overflow-x: auto;
        }

        .stat-item {
            display: flex;
            align-items: center;
            gap: 8px;
            padding: 8px 16px;
            background: var(--bg-card);
            border-radius: 6px;
            border: 1px solid var(--border);
            white-space: nowrap;
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
            grid-template-columns: 1fr 320px;
            gap: 20px;
        }

        @media (max-width: 1200px) {
            .main-layout {
                grid-template-columns: 1fr;
            }
        }

        /* 상담 테이블 */
        .chats-section {
            background: var(--bg-secondary);
            border-radius: 8px;
            overflow: hidden;
            border: 1px solid var(--border);
        }

        .table-header {
            padding: 12px 20px;
            border-bottom: 1px solid var(--border);
            background: var(--bg-card);
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
            background: var(--bg-hover);
        }

        .filter-tab.active {
            background: var(--ajd-blue);
            color: white;
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
            background: var(--bg-card);
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
            background: var(--bg-secondary);
            cursor: pointer;
            transition: background 0.1s;
        }

        .chat-table tr:hover {
            background: var(--bg-hover);
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
            font-weight: 500;
        }

        .assignee-cell.unassigned {
            color: var(--text-dim);
            font-style: italic;
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
            background: var(--bg-card);
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
            background: var(--bg-card);
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
            background: var(--border);
            border-radius: 4px;
        }

        ::-webkit-scrollbar-thumb:hover {
            background: var(--ajd-blue);
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
                <h1 class="title">⚡ 채널톡 실시간 모니터링</h1>
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
                                <th style="width: 120px;">담당자</th>
                                <th style="width: 100px;">팀</th>
                                <th style="width: 120px;">고객명</th>
                                <th>메시지</th>
                                <th style="width: 100px;">대기시간</th>
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
                    답변 랭킹 (담당 외)
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

        // 실시간 시간 업데이트
        function updateWaitTimes() {
            const now = new Date();
            allChats.forEach(chat => {
                if (chat.timestamp) {
                    const created = new Date(chat.timestamp);
                    const waitSeconds = Math.floor((now - created) / 1000);
                    chat.waitMinutes = Math.max(0, Math.floor(waitSeconds / 60));
                    chat.waitSeconds = Math.max(0, waitSeconds);
                }
            });
            renderTable();
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
                        <td colspan="6" class="empty-state">
                            <div class="empty-icon">✨</div>
                            <div class="empty-message">현재 대기 중인 상담이 없습니다</div>
                        </td>
                    </tr>
                `;
            } else {
                tbody.innerHTML = filteredChats.map(chat => {
                    const priority = getPriority(chat.waitMinutes);
                    const isUnassigned = chat.assignee === '미배정' || !chat.assignee;
                    const assigneeClass = isUnassigned ? 'assignee-cell unassigned' : 'assignee-cell';
                    
                    return `
                        <tr ondblclick="openChat('${chat.id}')">
                            <td><span class="priority-indicator ${priority}"></span></td>
                            <td class="${assigneeClass}">${chat.assignee || '미배정'}</td>
                            <td class="${assigneeClass}">${chat.team || '미배정'}</td>
                            <td class="customer-cell">${chat.customerName || '익명'}</td>
                            <td class="message-cell">${chat.lastMessage || '(메시지 없음)'}</td>
                            <td class="time-cell ${priority}">${formatWaitTime(chat.waitMinutes)}</td>
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
                selector.innerHTML += `<option value="${team}">${team}</option>`;
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
                } else if (data.type === 'time_update') {
                    updateWaitTimes();
                } else if (data.type === 'data_enriched') {
                    // API로 담당자 정보가 업데이트된 경우
                    console.log(`✅ ${data.count}개 상담의 담당자 정보가 업데이트되었습니다`);
                    fetchData();  // 전체 데이터 새로고침
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
        
        // 30초마다 시간 업데이트
        setInterval(updateWaitTimes, 30000);
        
        // 1분마다 데이터 동기화
        setInterval(fetchData, 60000);
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
        logger.info("⚡ 채널톡 실시간 모니터링 시스템 v2.0")
        logger.info(f"📌 대시보드: http://localhost:{PORT}")
        logger.info(f"🔌 WebSocket: ws://localhost:{PORT}/ws")
        logger.info(f"🎯 웹훅: http://localhost:{PORT}/webhook?token={WEBHOOK_TOKEN}")
        logger.info(f"🆔 채널톡 ID: {CHANNELTALK_ID}")
        
        # API 키 상태 확인
        if CHANNEL_API_KEY and CHANNEL_API_SECRET:
            logger.info("✅ Channel API 키 설정됨 - 담당자 정보 자동 조회 활성화")
        else:
            logger.warning("=" * 60)
            logger.warning("⚠️  Channel API 키가 설정되지 않았습니다!")
            logger.warning("    담당자 정보를 가져올 수 없어 모든 상담이 '미배정'으로 표시됩니다.")
            logger.warning("")
            logger.warning("📌 설정 방법:")
            logger.warning("1. https://desk.channel.io 접속")
            logger.warning("2. 설정 > 보안 및 개발자 > Open API")
            logger.warning("3. API 키 생성 후 환경변수 설정:")
            logger.warning("   export CHANNEL_API_KEY='your_key'")
            logger.warning("   export CHANNEL_API_SECRET='your_secret'")
            logger.warning("=" * 60)
        
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
    
    # API 키 확인
    if not CHANNEL_API_KEY or not CHANNEL_API_SECRET:
        print("\n" + "=" * 80)
        print("⚠️  경고: Channel API 키가 설정되지 않았습니다!")
        print("=" * 80)
        print("\n담당자 정보를 가져오려면 API 키가 필요합니다.")
        print("설정 없이 계속하면 모든 상담이 '미배정'으로 표시됩니다.\n")
        print("API 키 설정 방법:")
        print("1. https://desk.channel.io 접속")
        print("2. 설정 > 보안 및 개발자 > Open API")
        print("3. API 키 생성 후:")
        print("   export CHANNEL_API_KEY='your_key'")
        print("   export CHANNEL_API_SECRET='your_secret'")
        print("\n계속하시겠습니까? (y/n): ", end="")
        
        try:
            response = input().strip().lower()
            if response != 'y':
                print("프로그램을 종료합니다.")
                exit(0)
        except:
            pass
        
        print("\nAPI 키 없이 계속합니다...\n")
    
    async def main():
        app = await create_app()
        return app
    
    # 이벤트 루프 실행
    app = asyncio.run(main())
    web.run_app(app, host='0.0.0.0', port=PORT)
