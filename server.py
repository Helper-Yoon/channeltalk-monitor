import asyncio
import aiohttp
from aiohttp import web
import aioredis
import json
import os
from datetime import datetime
import logging
from typing import Dict, List, Optional
import weakref

# ===== 환경 변수 =====
REDIS_URL = os.getenv('REDIS_URL', 'redis://localhost:6379')
WEBHOOK_TOKEN = os.getenv('WEBHOOK_TOKEN', 'AJUNG')
PORT = int(os.getenv('PORT', 10000))

# ===== 로깅 설정 (상세 모드) =====
logging.basicConfig(
    level=logging.DEBUG,  # DEBUG 레벨로 변경
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

class ChannelTalkMonitor:
    """채널톡 미답변 상담 모니터링 시스템"""
    
    def __init__(self):
        self.redis: Optional[aioredis.Redis] = None
        self.memory_cache: Dict[str, dict] = {}
        self.websockets = weakref.WeakSet()
        logger.info("🚀 ChannelTalkMonitor 인스턴스 생성")
        
    async def setup(self):
        """초기 설정 - Redis 연결"""
        try:
            self.redis = await aioredis.create_redis_pool(
                REDIS_URL,
                encoding='utf-8',
                minsize=2,
                maxsize=10
            )
            logger.info("✅ Redis 연결 성공")
            
            # Redis 연결 테스트
            await self.redis.ping()
            logger.info("✅ Redis PING 성공")
            
        except Exception as e:
            logger.warning(f"⚠️ Redis 연결 실패, 메모리 모드로 실행: {e}")
            self.redis = None
    
    async def cleanup(self):
        """종료시 정리"""
        if self.redis:
            self.redis.close()
            await self.redis.wait_closed()
            logger.info("👋 Redis 연결 종료")
    
    # ===== 데이터 관리 =====
    
    async def save_chat(self, chat_data: dict):
        """미답변 상담 저장"""
        chat_id = chat_data['id']
        logger.info(f"💾 save_chat 호출: ID={chat_id}")
        
        if self.redis:
            try:
                # Redis에 저장 (1시간 TTL)
                key = f"chat:{chat_id}"
                value = json.dumps(chat_data)
                await self.redis.setex(key, 3600, value)
                await self.redis.sadd('unanswered_chats', chat_id)
                logger.info(f"✅ Redis 저장 성공: {key}")
                
                # 저장 확인
                test = await self.redis.get(key)
                if test:
                    logger.info(f"✅ 저장 확인: 데이터 존재")
                else:
                    logger.error(f"❌ 저장 실패: 데이터 없음")
                    
            except Exception as e:
                logger.error(f"❌ Redis 저장 실패: {e}")
                self.memory_cache[chat_id] = chat_data
                logger.info(f"💾 메모리 캐시로 저장: {chat_id}")
        else:
            # 메모리에 저장
            self.memory_cache[chat_id] = chat_data
            logger.info(f"💾 메모리 저장: {chat_id} (총 {len(self.memory_cache)}개)")
        
        logger.info(f"📢 브로드캐스트 시작: {chat_data['customerName']} - {chat_data['lastMessage'][:30]}...")
        
        # WebSocket으로 실시간 전송
        await self.broadcast({
            'type': 'new_chat',
            'chat': chat_data
        })
    
    async def remove_chat(self, chat_id: str):
        """답변 완료된 상담 제거"""
        logger.info(f"🗑️ remove_chat 호출: ID={chat_id}")
        
        if self.redis:
            try:
                result = await self.redis.delete(f"chat:{chat_id}")
                await self.redis.srem('unanswered_chats', chat_id)
                logger.info(f"✅ Redis에서 삭제: {chat_id} (결과: {result})")
            except Exception as e:
                logger.error(f"❌ Redis 삭제 실패: {e}")
                self.memory_cache.pop(chat_id, None)
        else:
            removed = self.memory_cache.pop(chat_id, None)
            if removed:
                logger.info(f"✅ 메모리에서 삭제: {chat_id}")
            else:
                logger.warning(f"⚠️ 삭제할 데이터 없음: {chat_id}")
        
        # WebSocket으로 실시간 전송
        await self.broadcast({
            'type': 'chat_answered',
            'chatId': chat_id
        })
    
    async def get_all_chats(self) -> List[dict]:
        """모든 미답변 채팅 조회"""
        logger.info("📊 get_all_chats 호출")
        chats = []
        
        if self.redis:
            try:
                # Redis에서 조회
                chat_ids = await self.redis.smembers('unanswered_chats')
                logger.info(f"📊 Redis에서 {len(chat_ids)}개 ID 발견")
                
                for chat_id in chat_ids:
                    key = f"chat:{chat_id}"
                    chat_json = await self.redis.get(key)
                    if chat_json:
                        chat_data = json.loads(chat_json)
                        chats.append(chat_data)
                        logger.debug(f"  ✅ {chat_id}: {chat_data['customerName']}")
                    else:
                        logger.warning(f"  ⚠️ {chat_id}의 데이터가 없음")
                        # 고아 ID 제거
                        await self.redis.srem('unanswered_chats', chat_id)
                        
            except Exception as e:
                logger.error(f"❌ Redis 조회 실패: {e}")
                chats = list(self.memory_cache.values())
                logger.info(f"📊 메모리 캐시 사용: {len(chats)}개")
        else:
            # 메모리에서 조회
            chats = list(self.memory_cache.values())
            logger.info(f"📊 메모리에서 {len(chats)}개 채팅 발견")
        
        # 대기시간 계산 및 정렬
        for chat in chats:
            try:
                created = datetime.fromisoformat(chat['timestamp'].replace('Z', '+00:00'))
                wait_minutes = int((datetime.utcnow() - created).total_seconds() / 60)
                chat['waitMinutes'] = max(0, wait_minutes)
            except Exception as e:
                logger.error(f"시간 계산 오류 {chat.get('id')}: {e}")
                chat['waitMinutes'] = 0
        
        # 대기시간 긴 순서로 정렬
        chats.sort(key=lambda x: x['waitMinutes'], reverse=True)
        
        logger.info(f"📊 최종 반환: {len(chats)}개 미답변 상담")
        if chats:
            logger.info(f"  첫번째: {chats[0]['customerName']} ({chats[0]['waitMinutes']}분 대기)")
        
        return chats
    
    # ===== 웹훅 처리 =====
    
    async def handle_webhook(self, request):
        """채널톡 웹훅 수신"""
        logger.info("=" * 50)
        logger.info("🔔 웹훅 요청 수신")
        
        # 토큰 검증
        tokens = request.query.getall('token', [])
        logger.info(f"  받은 토큰: {tokens}")
        
        valid_tokens = ['AJUNG', 'ajung', '80ab2d11835f44b89010c8efa5eec4b4', WEBHOOK_TOKEN]
        
        if not any(token.upper() in [t.upper() for t in valid_tokens] for token in tokens):
            logger.warning(f"❌ 잘못된 토큰: {tokens}")
            return web.Response(status=401)
        
        try:
            data = await request.json()
            event_type = data.get('type')
            
            logger.info(f"  이벤트 타입: {event_type}")
            logger.info(f"  최상위 키: {list(data.keys())}")
            
            # 이벤트 타입별 처리
            if event_type == 'message':
                await self.process_message(data)
            elif event_type == 'userChat':
                await self.process_user_chat(data)
            else:
                logger.warning(f"  ❓ 알 수 없는 이벤트 타입: {event_type}")
            
            logger.info("✅ 웹훅 처리 완료")
            logger.info("=" * 50)
            return web.json_response({"status": "ok"})
            
        except Exception as e:
            logger.error(f"❌ 웹훅 처리 오류: {e}", exc_info=True)
            return web.Response(status=500)
    
    async def process_message(self, data: dict):
        """메시지 이벤트 처리"""
        logger.info("📨 process_message 시작")
        
        try:
            # 전체 구조 확인
            logger.info(f"  데이터 키: {list(data.keys())}")
            
            # entity와 refers 추출
            entity = data.get('entity', {})
            refers = data.get('refers', {})
            
            # entity 내용 확인
            if entity:
                logger.info(f"  entity 키 (처음 15개): {list(entity.keys())[:15]}")
                logger.info(f"  entity.chatId: {entity.get('chatId')}")
                logger.info(f"  entity.personType: {entity.get('personType')}")
                logger.info(f"  entity.plainText: {entity.get('plainText', '')[:50]}")
            else:
                logger.warning("  ⚠️ entity가 비어있음!")
            
            # refers 내용 확인
            if refers:
                logger.info(f"  refers 키: {list(refers.keys())}")
                if 'user' in refers:
                    logger.info(f"    user 정보: {refers['user'].get('name', 'N/A')}")
                if 'userChat' in refers:
                    logger.info(f"    userChat 정보: {refers['userChat'].get('name', 'N/A')}")
            
            # 필요한 정보 추출
            chat_id = entity.get('chatId')
            person_type = entity.get('personType')
            plain_text = entity.get('plainText', '')
            created_at = entity.get('createdAt')
            
            logger.info(f"  추출 결과:")
            logger.info(f"    - chat_id: {chat_id}")
            logger.info(f"    - person_type: {person_type}")
            logger.info(f"    - plain_text: {plain_text[:50] if plain_text else '(없음)'}")
            logger.info(f"    - created_at: {created_at}")
            
            if not chat_id:
                logger.warning("  ⚠️ chat_id가 없어서 처리 중단!")
                return
            
            # 고객 메시지 처리
            if person_type == 'user':
                logger.info("  👤 고객 메시지로 판단됨!")
                
                # 고객 정보 추출
                user_info = refers.get('user', {})
                user_chat_info = refers.get('userChat', {})
                
                customer_name = (
                    user_info.get('name') or
                    user_info.get('username') or
                    user_chat_info.get('name') or
                    '익명'
                )
                
                logger.info(f"    고객명: {customer_name}")
                
                # 저장할 데이터
                chat_data = {
                    'id': str(chat_id),
                    'customerName': customer_name,
                    'lastMessage': plain_text or '(메시지 없음)',
                    'timestamp': created_at or datetime.utcnow().isoformat(),
                    'waitMinutes': 0
                }
                
                logger.info(f"  💾 저장 데이터 준비 완료:")
                logger.info(f"    {json.dumps(chat_data, ensure_ascii=False)}")
                
                await self.save_chat(chat_data)
                logger.info("  ✅ 고객 메시지 처리 완료")
                
            # 매니저/봇 답변 처리
            elif person_type in ['manager', 'bot']:
                logger.info(f"  🤖 {person_type} 답변으로 판단됨!")
                await self.remove_chat(str(chat_id))
                logger.info("  ✅ 답변 처리 완료")
            else:
                logger.warning(f"  ❓ 알 수 없는 person_type: {person_type}")
                
        except Exception as e:
            logger.error(f"❌ process_message 오류: {e}", exc_info=True)
            logger.error(f"  문제 데이터: {json.dumps(data, ensure_ascii=False)[:500]}")
    
    async def process_user_chat(self, data: dict):
        """유저챗 상태 변경 처리"""
        logger.info("💬 process_user_chat 시작")
        
        try:
            entity = data.get('entity', {})
            
            chat_id = entity.get('id')
            state = entity.get('state')
            
            logger.info(f"  chat_id: {chat_id}, state: {state}")
            
            # 상담 종료시 제거
            if state in ['closed', 'resolved'] and chat_id:
                logger.info(f"  상담 종료 감지: {chat_id}")
                await self.remove_chat(str(chat_id))
                logger.info("  ✅ 상담 종료 처리 완료")
                
        except Exception as e:
            logger.error(f"❌ process_user_chat 오류: {e}", exc_info=True)
    
    # ===== API 엔드포인트 =====
    
    async def get_chats(self, request):
        """미답변 상담 목록 API"""
        logger.info("📋 API: /api/chats 요청")
        
        try:
            chats = await self.get_all_chats()
            response_data = {
                'chats': chats,
                'total': len(chats),
                'timestamp': datetime.utcnow().isoformat()
            }
            
            logger.info(f"📋 API 응답: {len(chats)}개 상담 반환")
            return web.json_response(response_data)
            
        except Exception as e:
            logger.error(f"❌ API 오류: {e}", exc_info=True)
            return web.json_response({'chats': [], 'total': 0, 'error': str(e)})
    
    async def mark_answered(self, request):
        """수동으로 답변 완료 처리"""
        chat_id = request.match_info['chat_id']
        logger.info(f"🔨 수동 답변 완료: {chat_id}")
        await self.remove_chat(chat_id)
        return web.json_response({'status': 'ok'})
    
    async def health_check(self, request):
        """헬스 체크"""
        chats = await self.get_all_chats()
        health_data = {
            'status': 'healthy',
            'redis': 'connected' if self.redis else 'memory_mode',
            'unanswered_count': len(chats),
            'websocket_connections': len(self.websockets),
            'memory_cache_count': len(self.memory_cache),
            'timestamp': datetime.utcnow().isoformat()
        }
        logger.info(f"🏥 헬스체크: {health_data}")
        return web.json_response(health_data)
    
    # ===== WebSocket =====
    
    async def handle_websocket(self, request):
        """WebSocket 연결 처리"""
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        
        self.websockets.add(ws)
        logger.info(f"🔌 WebSocket 연결 (총 {len(self.websockets)}개)")
        
        try:
            # 초기 데이터 전송
            chats = await self.get_all_chats()
            await ws.send_json({
                'type': 'initial',
                'chats': chats
            })
            logger.info(f"  초기 데이터 전송: {len(chats)}개")
            
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
            logger.info(f"🔌 WebSocket 해제 (남은 연결: {len(self.websockets)}개)")
        
        return ws
    
    async def broadcast(self, data: dict):
        """모든 WebSocket 클라이언트에 전송"""
        if not self.websockets:
            logger.info("  브로드캐스트: 연결된 클라이언트 없음")
            return
            
        logger.info(f"📢 브로드캐스트: {data['type']} to {len(self.websockets)} clients")
        dead = []
        
        for ws in self.websockets:
            try:
                await ws.send_json(data)
                logger.debug(f"  ✅ 전송 성공")
            except Exception as e:
                logger.error(f"  ❌ 전송 실패: {e}")
                dead.append(ws)
        
        for ws in dead:
            self.websockets.discard(ws)
    
    # ===== 대시보드 =====
    
    async def serve_dashboard(self, request):
        """대시보드 HTML 서빙"""
        logger.info("🌐 대시보드 요청")
        return web.Response(text=DASHBOARD_HTML, content_type='text/html')

# ===== 대시보드 HTML =====
DASHBOARD_HTML = """
<!DOCTYPE html>
<html lang="ko">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>채널톡 미답변 상담 모니터 프로그램</title>
    <style>
        :root {
            --bg-primary: #0a0a0a;
            --bg-secondary: #1a1a1a;
            --bg-card: #242424;
            --text-primary: #ffffff;
            --text-secondary: #a0a0a0;
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
            background: var(--bg-primary);
            color: var(--text-primary);
            min-height: 100vh;
            padding: 20px;
        }

        .container {
            max-width: 1400px;
            margin: 0 auto;
        }

        /* 헤더 */
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

        /* 통계 */
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

        /* 컨트롤 */
        .controls {
            display: flex;
            gap: 12px;
            margin-bottom: 24px;
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

        /* 채팅 그리드 */
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
        }

        .status-dot {
            width: 8px;
            height: 8px;
            border-radius: 50%;
            background: var(--status-new);
            animation: pulse 2s infinite;
        }

        @keyframes pulse {
            0%, 100% { opacity: 1; }
            50% { opacity: 0.5; }
        }

        /* 디버그 패널 */
        .debug-panel {
            background: var(--bg-secondary);
            border: 1px solid #333;
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
        <span id="connectionText">연결됨</span>
    </div>

    <div class="container">
        <div class="header">
            <h1 class="title">🔷 채널톡 미답변 상담 모니터 프로그램</h1>
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
            <button class="btn" id="autoRefreshBtn" onclick="toggleAutoRefresh()">⏸️ 자동새로고침</button>
            <button class="btn" onclick="showDebug()">🔍 디버그 정보</button>
        </div>

        <div class="debug-panel" id="debugPanel" style="display: none;">
            <div id="debugInfo">디버그 정보 로딩 중...</div>
        </div>

        <div class="chat-grid" id="chatGrid">
            <!-- 여기에 채팅 카드가 동적으로 생성됩니다 -->
        </div>
    </div>

    <script>
        let ws = null;
        let chats = [];
        let autoRefresh = true;
        let refreshInterval;

        // 우선순위 결정
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

        // 렌더링
        function renderChats() {
            const grid = document.getElementById('chatGrid');
            
            console.log('렌더링 시작, 채팅 수:', chats.length);
            
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

        // 통계 업데이트
        function updateStats() {
            document.getElementById('totalCount').textContent = chats.length;
            document.getElementById('criticalCount').textContent = chats.filter(c => c.waitMinutes >= 11).length;
            document.getElementById('warningCount').textContent = chats.filter(c => c.waitMinutes >= 8 && c.waitMinutes < 11).length;
            document.getElementById('cautionCount').textContent = chats.filter(c => c.waitMinutes >= 5 && c.waitMinutes < 8).length;
            document.getElementById('normalCount').textContent = chats.filter(c => c.waitMinutes >= 2 && c.waitMinutes < 5).length;
            document.getElementById('newCount').textContent = chats.filter(c => c.waitMinutes < 2).length;
        }

        // WebSocket 연결
        function connectWebSocket() {
            const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
            const wsUrl = `${protocol}//${window.location.host}/ws`;
            console.log('WebSocket 연결 시도:', wsUrl);
            
            ws = new WebSocket(wsUrl);
            
            ws.onopen = () => {
                console.log('✅ WebSocket 연결됨');
                document.getElementById('connectionText').textContent = '실시간 연결됨';
            };
            
            ws.onmessage = (event) => {
                const data = JSON.parse(event.data);
                console.log('WebSocket 메시지:', data);
                
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
                console.error('❌ WebSocket 오류:', error);
                document.getElementById('connectionText').textContent = '폴링 모드';
                fetchData();
            };
            
            ws.onclose = () => {
                console.log('WebSocket 연결 끊김');
                document.getElementById('connectionText').textContent = '재연결 중...';
                setTimeout(connectWebSocket, 5000);
            };
        }

        // API로 데이터 가져오기
        async function fetchData() {
            try {
                console.log('API 호출 시작');
                const response = await fetch('/api/chats');
                const data = await response.json();
                console.log('API 응답:', data);
                chats = data.chats;
                renderChats();
            } catch (error) {
                console.error('데이터 로드 실패:', error);
            }
        }

        // 새로고침
        function refreshData() {
            fetchData();
        }

        // 자동 새로고침 토글
        function toggleAutoRefresh() {
            autoRefresh = !autoRefresh;
            const btn = document.getElementById('autoRefreshBtn');
            
            if (autoRefresh) {
                btn.textContent = '⏸️ 자동새로고침';
                refreshInterval = setInterval(fetchData, 5000);
            } else {
                btn.textContent = '▶️ 자동새로고침';
                clearInterval(refreshInterval);
            }
        }

        // 답변 완료
        async function markAnswered(chatId) {
            await fetch(`/api/chats/${chatId}/answer`, { method: 'POST' });
            chats = chats.filter(c => c.id !== chatId);
            renderChats();
        }

        // 디버그 정보 표시
        async function showDebug() {
            const panel = document.getElementById('debugPanel');
            const info = document.getElementById('debugInfo');
            
            if (panel.style.display === 'none') {
                panel.style.display = 'block';
                
                try {
                    const response = await fetch('/health');
                    const data = await response.json();
                    
                    info.innerHTML = `
                        <strong>시스템 상태:</strong><br>
                        - Redis: ${data.redis}<br>
                        - 미답변 상담: ${data.unanswered_count}개<br>
                        - WebSocket 연결: ${data.websocket_connections}개<br>
                        - 메모리 캐시: ${data.memory_cache_count}개<br>
                        - 시간: ${new Date(data.timestamp).toLocaleString('ko-KR')}<br>
                        <br>
                        <strong>현재 로드된 상담:</strong> ${chats.length}개<br>
                        <strong>WebSocket 상태:</strong> ${ws ? ws.readyState : 'N/A'}<br>
                    `;
                } catch (error) {
                    info.textContent = '디버그 정보를 가져올 수 없습니다: ' + error;
                }
            } else {
                panel.style.display = 'none';
            }
        }

        // 초기화
        console.log('페이지 로드 완료, 초기화 시작');
        connectWebSocket();
        fetchData();
        refreshInterval = setInterval(fetchData, 5000);
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
    
    logger.info("✅ 라우트 설정 완료")
    
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
    
    # 시작/종료 핸들러
    async def on_startup(app):
        logger.info("=" * 50)
        logger.info("🚀 채널톡 미답변 상담 모니터 시작")
        logger.info(f"📌 대시보드: http://localhost:{PORT}")
        logger.info(f"📌 API: http://localhost:{PORT}/api/chats")
        logger.info(f"📌 헬스체크: http://localhost:{PORT}/health")
        logger.info("=" * 50)
    
    async def on_cleanup(app):
        await monitor.cleanup()
        logger.info("👋 서버 종료")
    
    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    
    logger.info("✅ 애플리케이션 생성 완료")
    return app

# ===== 메인 실행 =====
if __name__ == '__main__':
    logger.info("🏁 프로그램 시작")
    loop = asyncio.get_event_loop()
    app = loop.run_until_complete(create_app())
    web.run_app(app, host='0.0.0.0', port=PORT)
