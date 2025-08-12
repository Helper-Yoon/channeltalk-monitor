"""
로컬 개발 및 테스트를 위한 스크립트
Redis와 FastAPI를 로컬에서 실행
"""

# test_local.py
import asyncio
import json
import aiohttp
from datetime import datetime
import random
import time

# 테스트 데이터 생성기
class TestDataGenerator:
    def __init__(self):
        self.names = ["김철수", "이영희", "박민수", "정수진", "최동욱"]
        self.teams = ["SNS 1팀", "SNS 2팀", "SNS 3팀", "SNS 4팀", "의정부 SNS팀", "미배정"]
        self.messages = [
            "정수기 렌탈 문의드립니다",
            "인터넷 설치 가능한가요?",
            "휴대폰 요금제 변경하고 싶어요",
            "TV 채널 추가 문의",
            "해지 후 재가입 혜택 있나요?",
            "사은품은 언제 받을 수 있나요?",
            "A/S 신청하고 싶습니다",
            "요금 확인 부탁드립니다"
        ]
    
    def generate_chat(self):
        """랜덤 채팅 데이터 생성"""
        chat_id = f"test_{int(time.time())}_{random.randint(1000, 9999)}"
        
        return {
            "type": "message",
            "data": {
                "userChat": {
                    "id": chat_id,
                    "assignee": {
                        "id": f"manager_{random.randint(100, 999)}",
                        "name": random.choice(self.names),
                        "teams": [{"name": random.choice(self.teams)}]
                    },
                    "user": {
                        "profile": {
                            "name": f"고객_{random.randint(100, 999)}",
                            "mobileNumber": f"010-{random.randint(1000,9999)}-{random.randint(1000,9999)}"
                        }
                    }
                },
                "message": {
                    "plainText": random.choice(self.messages)
                },
                "personType": "user"
            }
        }
    
    def generate_answer(self, chat_id):
        """답변 이벤트 생성"""
        return {
            "type": "message",
            "data": {
                "userChat": {"id": chat_id},
                "person": {"name": random.choice(self.names)},
                "personType": "manager"
            }
        }

# 웹훅 테스터
class WebhookTester:
    def __init__(self, base_url="http://localhost:8000"):
        self.base_url = base_url
        self.token = "80ab2d11835f44b89010c8efa5eec4b4"
        self.generator = TestDataGenerator()
        self.active_chats = []
    
    async def send_webhook(self, data):
        """웹훅 전송"""
        url = f"{self.base_url}/webhook?token={self.token}"
        
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=data) as response:
                result = await response.json()
                print(f"✅ 웹훅 전송: {response.status} - {result}")
                return response.status == 200
    
    async def create_chat(self):
        """새 상담 생성"""
        chat_data = self.generator.generate_chat()
        chat_id = chat_data["data"]["userChat"]["id"]
        
        if await self.send_webhook(chat_data):
            self.active_chats.append(chat_id)
            print(f"📝 새 상담 생성: {chat_id}")
            return chat_id
        return None
    
    async def answer_chat(self, chat_id=None):
        """상담 답변"""
        if not chat_id and self.active_chats:
            chat_id = random.choice(self.active_chats)
        
        if chat_id:
            answer_data = self.generator.generate_answer(chat_id)
            
            if await self.send_webhook(answer_data):
                self.active_chats.remove(chat_id)
                print(f"💬 상담 답변: {chat_id}")
                return True
        return False
    
    async def run_simulation(self, duration=60):
        """시뮬레이션 실행"""
        print(f"🚀 {duration}초 동안 시뮬레이션 시작")
        start_time = time.time()
        
        while time.time() - start_time < duration:
            # 70% 확률로 새 상담, 30% 확률로 답변
            if random.random() < 0.7:
                await self.create_chat()
            elif self.active_chats:
                await self.answer_chat()
            
            # 1-5초 대기
            await asyncio.sleep(random.uniform(1, 5))
        
        print(f"✅ 시뮬레이션 완료")
        print(f"📊 생성된 상담: {len(self.active_chats)}개 대기 중")

# WebSocket 테스터
class WebSocketTester:
    def __init__(self, ws_url="ws://localhost:8000/ws"):
        self.ws_url = ws_url
    
    async def connect_and_listen(self):
        """WebSocket 연결 및 메시지 수신"""
        import websockets
        
        print(f"🔌 WebSocket 연결 중: {self.ws_url}")
        
        async with websockets.connect(self.ws_url) as websocket:
            print("✅ WebSocket 연결 성공")
            
            # 핑 전송 태스크
            async def send_ping():
                while True:
                    await asyncio.sleep(30)
                    await websocket.send("ping")
                    print("🏓 Ping 전송")
            
            ping_task = asyncio.create_task(send_ping())
            
            try:
                while True:
                    message = await websocket.recv()
                    
                    if message == "pong":
                        print("🏓 Pong 수신")
                    else:
                        data = json.loads(message)
                        print(f"📨 메시지 수신: {data.get('type')}")
                        
                        if data.get('type') == 'initial_data':
                            print(f"   상담 {len(data.get('chats', []))}개")
                        elif data.get('type') == 'new_chat':
                            chat = data.get('chat', {})
                            print(f"   새 상담: {chat.get('customer_phone')}")
                        elif data.get('type') == 'chat_answered':
                            print(f"   답변 완료: {data.get('chat_id')}")
                            
            except KeyboardInterrupt:
                print("\n👋 연결 종료")
                ping_task.cancel()

# API 테스터
class APITester:
    def __init__(self, base_url="http://localhost:8000"):
        self.base_url = base_url
    
    async def test_endpoints(self):
        """모든 엔드포인트 테스트"""
        endpoints = [
            ("/health", "GET"),
            ("/api/chats", "GET"),
            ("/api/stats", "GET"),
            ("/api/chats?team=SNS 1팀", "GET")
        ]
        
        async with aiohttp.ClientSession() as session:
            for endpoint, method in endpoints:
                url = f"{self.base_url}{endpoint}"
                
                try:
                    async with session.request(method, url) as response:
                        data = await response.json()
                        status = "✅" if response.status == 200 else "❌"
                        print(f"{status} {method} {endpoint}: {response.status}")
                        
                        if endpoint == "/health":
                            print(f"   Redis: {data.get('redis')}")
                            print(f"   Connections: {data.get('connections')}")
                        elif endpoint == "/api/stats":
                            print(f"   Total waiting: {data.get('total_waiting')}")
                            print(f"   Urgent: {data.get('urgent_count')}")
                            
                except Exception as e:
                    print(f"❌ {method} {endpoint}: {e}")

# 부하 테스터
class LoadTester:
    def __init__(self, base_url="http://localhost:8000"):
        self.base_url = base_url
        self.token = "80ab2d11835f44b89010c8efa5eec4b4"
    
    async def send_request(self, session, data):
        """단일 요청 전송"""
        url = f"{self.base_url}/webhook?token={self.token}"
        
        start = time.time()
        try:
            async with session.post(url, json=data) as response:
                duration = time.time() - start
                return response.status, duration
        except Exception as e:
            return 500, time.time() - start
    
    async def run_load_test(self, num_requests=100, concurrent=10):
        """부하 테스트 실행"""
        print(f"🔥 부하 테스트 시작: {num_requests}개 요청, 동시 {concurrent}개")
        
        generator = TestDataGenerator()
        results = []
        
        async with aiohttp.ClientSession() as session:
            for i in range(0, num_requests, concurrent):
                batch = []
                
                for j in range(min(concurrent, num_requests - i)):
                    data = generator.generate_chat()
                    batch.append(self.send_request(session, data))
                
                batch_results = await asyncio.gather(*batch)
                results.extend(batch_results)
                
                print(f"   진행: {i + len(batch)}/{num_requests}")
        
        # 결과 분석
        success = sum(1 for status, _ in results if status == 200)
        avg_duration = sum(duration for _, duration in results) / len(results)
        max_duration = max(duration for _, duration in results)
        min_duration = min(duration for _, duration in results)
        
        print(f"\n📊 부하 테스트 결과:")
        print(f"   성공률: {success}/{num_requests} ({success*100/num_requests:.1f}%)")
        print(f"   평균 응답시간: {avg_duration*1000:.2f}ms")
        print(f"   최대 응답시간: {max_duration*1000:.2f}ms")
        print(f"   최소 응답시간: {min_duration*1000:.2f}ms")

# 메인 테스트 실행
async def main():
    print("""
    🧪 아정당 채널톡 모니터링 테스트 도구
    
    1. API 엔드포인트 테스트
    2. 웹훅 시뮬레이션 (60초)
    3. WebSocket 연결 테스트
    4. 부하 테스트 (100 요청)
    5. 전체 테스트
    
    선택: """, end="")
    
    choice = input().strip()
    
    if choice == "1":
        tester = APITester()
        await tester.test_endpoints()
        
    elif choice == "2":
        tester = WebhookTester()
        await tester.run_simulation(60)
        
    elif choice == "3":
        tester = WebSocketTester()
        await tester.connect_and_listen()
        
    elif choice == "4":
        tester = LoadTester()
        await tester.run_load_test(100, 10)
        
    elif choice == "5":
        print("\n=== API 테스트 ===")
        api_tester = APITester()
        await api_tester.test_endpoints()
        
        print("\n=== 부하 테스트 ===")
        load_tester = LoadTester()
        await load_tester.run_load_test(50, 5)
        
        print("\n=== 시뮬레이션 ===")
        webhook_tester = WebhookTester()
        await webhook_tester.run_simulation(30)
    
    else:
        print("잘못된 선택입니다.")

if __name__ == "__main__":
    asyncio.run(main())

---
# docker-compose.local.yml
# 로컬 개발용 Docker Compose
version: '3.8'

services:
  redis:
    image: redis:7-alpine
    ports:
      - "6379:6379"
    command: redis-server --maxmemory 256mb --maxmemory-policy allkeys-lru
    volumes:
      - redis-data:/data
    healthcheck:
      test: ["CMD", "redis-cli", "ping"]
      interval: 5s
      timeout: 3s
      retries: 5

  app:
    build: .
    ports:
      - "8000:8000"
    environment:
      - REDIS_URL=redis://redis:6379
      - CHANNEL_WEBHOOK_TOKEN=80ab2d11835f44b89010c8efa5eec4b4
      - CORS_ORIGINS=http://localhost:8000,http://localhost:3000
      - PORT=8000
    depends_on:
      redis:
        condition: service_healthy
    volumes:
      - ./:/app
      - ./static:/app/static
    command: uvicorn main:app --host 0.0.0.0 --port 8000 --reload

volumes:
  redis-data:

---
# Makefile
.PHONY: help install dev test deploy clean

help:
	@echo "사용 가능한 명령어:"
	@echo "  make install  - 의존성 설치"
	@echo "  make dev      - 로컬 개발 서버 실행"
	@echo "  make test     - 테스트 실행"
	@echo "  make deploy   - Render 배포"
	@echo "  make clean    - 정리"

install:
	pip install -r requirements.txt
	pip install websockets aiohttp pytest

dev:
	# Redis 시작
	docker run -d --name redis-dev -p 6379:6379 redis:7-alpine
	# 앱 실행
	uvicorn main:app --reload --port 8000

dev-docker:
	docker-compose -f docker-compose.local.yml up

test:
	python test_local.py

deploy:
	git add .
	git commit -m "Deploy to Render"
	git push origin main
	@echo "✅ Render에서 자동 배포가 시작됩니다."
	@echo "📊 https://dashboard.render.com 에서 진행 상황을 확인하세요."

logs:
	# Render CLI 필요
	render logs -s ajeongdang-channeltalk-monitor --tail

clean:
	docker stop redis-dev 2>/dev/null || true
	docker rm redis-dev 2>/dev/null || true
	docker-compose -f docker-compose.local.yml down -v
	find . -type d -name "__pycache__" -exec rm -r {} + 2>/dev/null || true
	find . -type f -name "*.pyc" -delete

redis-cli:
	docker exec -it redis-dev redis-cli

monitoring:
	@echo "📊 모니터링 URL:"
	@echo "로컬: http://localhost:8000"
	@echo "프로덕션: https://ajeongdang-channeltalk-monitor.onrender.com"
