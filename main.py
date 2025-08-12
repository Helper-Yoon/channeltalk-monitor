#!/bin/bash

# 📁 프로젝트 구조
# channeltalk-monitor/
# ├── main.py           # 메인 서버 파일
# ├── requirements.txt  # Python 의존성
# ├── render.yaml      # Render 설정 (선택사항)
# └── README.md        # 문서

# 1️⃣ requirements.txt 생성
cat > requirements.txt << 'EOF'
aiohttp==3.9.1
redis[hiredis]==5.0.1
EOF

# 2️⃣ render.yaml 생성 (선택사항 - 자동 설정용)
cat > render.yaml << 'EOF'
services:
  - type: web
    name: ajeongdang-channeltalk
    runtime: python
    plan: starter  # 또는 pro
    region: singapore
    buildCommand: pip install -r requirements.txt
    startCommand: python main.py
    envVars:
      - key: PYTHON_VERSION
        value: 3.11.0
      - key: CHANNEL_WEBHOOK_TOKEN
        value: 80ab2d11835f44b89010c8efa5eec4b4
      - key: REDIS_URL
        fromDatabase:
          name: ajeongdang-redis
          property: connectionString
    healthCheckPath: /health

  - type: redis
    name: ajeongdang-redis
    plan: starter  # 또는 standard
    region: singapore
    maxmemoryPolicy: allkeys-lru
EOF

# 3️⃣ README.md 생성
cat > README.md << 'EOF'
# 아정당 채널톡 모니터링 시스템

## 🚀 Render 배포 가이드

### 빠른 시작

1. **GitHub에 푸시**
```bash
git add .
git commit -m "Initial deployment"
git push origin main
```

2. **Render에서 배포**
- [Render Dashboard](https://dashboard.render.com) 접속
- New → Web Service 클릭
- GitHub 저장소 연결
- 설정:
  - Name: `ajeongdang-channeltalk`
  - Region: Singapore
  - Runtime: Python 3
  - Build Command: `pip install -r requirements.txt`
  - Start Command: `python main.py`
  - Plan: Starter 또는 Pro

3. **Redis 추가 (선택사항)**
- New → Redis 클릭
- Name: `ajeongdang-redis`
- Region: Singapore (웹 서비스와 동일)
- Plan: Starter 또는 Standard

4. **환경 변수 설정**
- `REDIS_URL`: Redis 연결 URL (자동 설정됨)
- `CHANNEL_WEBHOOK_TOKEN`: 80ab2d11835f44b89010c8efa5eec4b4

### 📊 모니터링 URL

배포 후 접속:
- 대시보드: `https://[your-app].onrender.com`
- API: `https://[your-app].onrender.com/api/chats`
- 헬스체크: `https://[your-app].onrender.com/health`

### 🔧 채널톡 웹훅 설정

채널톡 관리자 페이지에서:
```
https://[your-app].onrender.com/webhook?token=80ab2d11835f44b89010c8efa5eec4b4
```

### ⚡ 성능

- WebSocket 실시간 연결
- Redis 캐싱 (선택사항)
- 자동 재연결
- 4시간 이상 오래된 데이터 자동 정리

### 🐛 트러블슈팅

**Redis 연결 실패 시:**
- 앱은 메모리 캐시로 자동 전환
- 서버 재시작 시 데이터 손실 주의

**WebSocket 연결 문제:**
- 자동 재연결 (5초 간격)
- 폴백으로 10초마다 API 호출

### 📈 업그레이드

**더 많은 성능이 필요한 경우:**
1. Web Service: Starter → Pro
2. Redis: Starter → Standard
3. 리전: Singapore (한국과 가장 가까움)
EOF

# 4️⃣ .gitignore 생성
cat > .gitignore << 'EOF'
__pycache__/
*.py[cod]
*$py.class
*.so
.Python
env/
venv/
ENV/
.env
.vscode/
.idea/
*.log
EOF

# 5️⃣ 배포 스크립트
cat > deploy.sh << 'EOF'
#!/bin/bash

echo "🚀 Render 배포 준비 중..."

# Git 초기화 확인
if [ ! -d .git ]; then
    git init
    git add .
    git commit -m "Initial commit"
    echo "✅ Git 저장소 초기화 완료"
fi

# 변경사항 커밋
git add .
git commit -m "Update deployment files"

# 원격 저장소 확인
if ! git remote | grep -q origin; then
    echo "⚠️  GitHub 원격 저장소를 추가해주세요:"
    echo "git remote add origin https://github.com/YOUR_USERNAME/YOUR_REPO.git"
    exit 1
fi

# 푸시
git push origin main

echo "✅ GitHub에 푸시 완료!"
echo ""
echo "📝 다음 단계:"
echo "1. https://dashboard.render.com 접속"
echo "2. 'New' → 'Web Service' 클릭"
echo "3. GitHub 저장소 선택"
echo "4. 설정 확인 후 'Create Web Service' 클릭"
echo ""
echo "🎉 배포가 자동으로 시작됩니다!"
EOF

chmod +x deploy.sh

echo "✅ 모든 파일이 생성되었습니다!"
echo ""
echo "📁 생성된 파일:"
echo "  - main.py (위에서 제공한 파일 사용)"
echo "  - requirements.txt"
echo "  - render.yaml (선택사항)"
echo "  - README.md"
echo "  - .gitignore"
echo "  - deploy.sh"
echo ""
echo "🚀 배포 방법:"
echo "  1. main.py 파일을 위에서 제공한 내용으로 교체"
echo "  2. ./deploy.sh 실행"
echo "  3. Render Dashboard에서 배포 진행"
