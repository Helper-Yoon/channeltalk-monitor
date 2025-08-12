<!DOCTYPE html>
<html lang="ko">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>아정당 실시간 상담 모니터링</title>
    <style>
        :root {
            --ajungdang-blue: #0066CC;
            --ajungdang-blue-light: #3399FF;
            --ajungdang-blue-dark: #004499;
            --bg-primary: #0A0E1A;
            --bg-secondary: #151B2C;
            --bg-card: #1C2333;
            --bg-hover: #242B3D;
            --text-primary: #E8EAED;
            --text-secondary: #9CA3AF;
            --text-muted: #6B7280;
            --border-color: #2A3142;
            --success: #10B981;
            --warning: #F59E0B;
            --danger: #EF4444;
            --gradient-blue: linear-gradient(135deg, var(--ajungdang-blue) 0%, var(--ajungdang-blue-light) 100%);
        }

        * {
            margin: 0;
            padding: 0;
            box-sizing: border-box;
        }

        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, 'Helvetica Neue', Arial, sans-serif;
            background: var(--bg-primary);
            color: var(--text-primary);
            line-height: 1.6;
            overflow-x: hidden;
        }

        /* 헤더 */
        .header {
            background: linear-gradient(180deg, var(--bg-secondary) 0%, rgba(21, 27, 44, 0) 100%);
            padding: 2rem 0;
            position: fixed;
            top: 0;
            left: 0;
            right: 0;
            z-index: 1000;
            backdrop-filter: blur(20px);
            border-bottom: 1px solid var(--border-color);
        }

        .header-content {
            max-width: 1400px;
            margin: 0 auto;
            padding: 0 2rem;
            display: flex;
            justify-content: space-between;
            align-items: center;
        }

        .logo-section {
            display: flex;
            align-items: center;
            gap: 1rem;
        }

        .logo {
            width: 40px;
            height: 40px;
            background: var(--gradient-blue);
            border-radius: 12px;
            display: flex;
            align-items: center;
            justify-content: center;
            font-weight: bold;
            font-size: 1.2rem;
            box-shadow: 0 4px 20px rgba(0, 102, 204, 0.3);
        }

        .title {
            font-size: 1.5rem;
            font-weight: 700;
            background: var(--gradient-blue);
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
        }

        .connection-status {
            display: flex;
            align-items: center;
            gap: 0.5rem;
            padding: 0.5rem 1rem;
            background: var(--bg-card);
            border-radius: 20px;
            border: 1px solid var(--border-color);
        }

        .status-dot {
            width: 8px;
            height: 8px;
            border-radius: 50%;
            background: var(--success);
            animation: pulse 2s infinite;
        }

        @keyframes pulse {
            0%, 100% { opacity: 1; }
            50% { opacity: 0.5; }
        }

        .status-dot.disconnected {
            background: var(--danger);
            animation: none;
        }

        /* 메인 컨테이너 */
        .main-container {
            max-width: 1400px;
            margin: 7rem auto 2rem;
            padding: 0 2rem;
        }

        /* 통계 카드 */
        .stats-grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(250px, 1fr));
            gap: 1.5rem;
            margin-bottom: 2rem;
        }

        .stat-card {
            background: var(--bg-card);
            border: 1px solid var(--border-color);
            border-radius: 16px;
            padding: 1.5rem;
            position: relative;
            overflow: hidden;
            transition: all 0.3s ease;
        }

        .stat-card:hover {
            transform: translateY(-2px);
            border-color: var(--ajungdang-blue);
            box-shadow: 0 8px 32px rgba(0, 102, 204, 0.2);
        }

        .stat-card::before {
            content: '';
            position: absolute;
            top: 0;
            left: 0;
            right: 0;
            height: 3px;
            background: var(--gradient-blue);
        }

        .stat-label {
            color: var(--text-secondary);
            font-size: 0.875rem;
            margin-bottom: 0.5rem;
            text-transform: uppercase;
            letter-spacing: 0.5px;
        }

        .stat-value {
            font-size: 2rem;
            font-weight: 700;
            color: var(--text-primary);
            display: flex;
            align-items: baseline;
            gap: 0.5rem;
        }

        .stat-change {
            font-size: 0.875rem;
            padding: 0.25rem 0.5rem;
            border-radius: 6px;
            background: rgba(16, 185, 129, 0.1);
            color: var(--success);
        }

        .stat-change.negative {
            background: rgba(239, 68, 68, 0.1);
            color: var(--danger);
        }

        /* 상담 리스트 */
        .consultations-section {
            background: var(--bg-card);
            border: 1px solid var(--border-color);
            border-radius: 16px;
            overflow: hidden;
        }

        .section-header {
            padding: 1.5rem;
            background: var(--bg-secondary);
            border-bottom: 1px solid var(--border-color);
            display: flex;
            justify-content: space-between;
            align-items: center;
        }

        .section-title {
            font-size: 1.25rem;
            font-weight: 600;
            display: flex;
            align-items: center;
            gap: 0.5rem;
        }

        .filter-buttons {
            display: flex;
            gap: 0.5rem;
        }

        .filter-btn {
            padding: 0.5rem 1rem;
            background: var(--bg-card);
            border: 1px solid var(--border-color);
            border-radius: 8px;
            color: var(--text-secondary);
            cursor: pointer;
            transition: all 0.2s ease;
            font-size: 0.875rem;
        }

        .filter-btn:hover {
            background: var(--bg-hover);
            color: var(--text-primary);
        }

        .filter-btn.active {
            background: var(--ajungdang-blue);
            color: white;
            border-color: var(--ajungdang-blue);
        }

        .consultations-list {
            max-height: 600px;
            overflow-y: auto;
        }

        .consultations-list::-webkit-scrollbar {
            width: 8px;
        }

        .consultations-list::-webkit-scrollbar-track {
            background: var(--bg-secondary);
        }

        .consultations-list::-webkit-scrollbar-thumb {
            background: var(--ajungdang-blue);
            border-radius: 4px;
        }

        .consultation-item {
            padding: 1.5rem;
            border-bottom: 1px solid var(--border-color);
            display: flex;
            justify-content: space-between;
            align-items: center;
            transition: all 0.2s ease;
            cursor: pointer;
            animation: slideIn 0.3s ease;
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

        .consultation-item:hover {
            background: var(--bg-hover);
        }

        .consultation-item.new {
            border-left: 3px solid var(--ajungdang-blue);
            background: linear-gradient(90deg, rgba(0, 102, 204, 0.1) 0%, transparent 100%);
        }

        .consultation-info {
            flex: 1;
        }

        .consultation-id {
            font-size: 0.875rem;
            color: var(--text-secondary);
            margin-bottom: 0.25rem;
            font-family: 'Courier New', monospace;
        }

        .consultation-customer {
            font-size: 1.125rem;
            font-weight: 500;
            color: var(--text-primary);
            margin-bottom: 0.5rem;
        }

        .consultation-meta {
            display: flex;
            gap: 1rem;
            font-size: 0.875rem;
            color: var(--text-muted);
        }

        .meta-item {
            display: flex;
            align-items: center;
            gap: 0.25rem;
        }

        .consultation-status {
            display: flex;
            flex-direction: column;
            align-items: flex-end;
            gap: 0.5rem;
        }

        .status-badge {
            padding: 0.375rem 0.75rem;
            border-radius: 20px;
            font-size: 0.75rem;
            font-weight: 500;
            text-transform: uppercase;
            letter-spacing: 0.5px;
        }

        .status-badge.waiting {
            background: rgba(245, 158, 11, 0.1);
            color: var(--warning);
            border: 1px solid var(--warning);
        }

        .status-badge.urgent {
            background: rgba(239, 68, 68, 0.1);
            color: var(--danger);
            border: 1px solid var(--danger);
            animation: blink 1s infinite;
        }

        @keyframes blink {
            0%, 100% { opacity: 1; }
            50% { opacity: 0.5; }
        }

        .time-waiting {
            font-size: 0.875rem;
            color: var(--text-secondary);
        }

        /* 액션 버튼 */
        .action-btn {
            padding: 0.5rem 1rem;
            background: var(--gradient-blue);
            border: none;
            border-radius: 8px;
            color: white;
            font-weight: 500;
            cursor: pointer;
            transition: all 0.2s ease;
            box-shadow: 0 4px 12px rgba(0, 102, 204, 0.3);
        }

        .action-btn:hover {
            transform: translateY(-1px);
            box-shadow: 0 6px 20px rgba(0, 102, 204, 0.4);
        }

        /* 빈 상태 */
        .empty-state {
            padding: 4rem 2rem;
            text-align: center;
            color: var(--text-secondary);
        }

        .empty-icon {
            font-size: 3rem;
            margin-bottom: 1rem;
            opacity: 0.5;
        }

        /* 실시간 인디케이터 */
        .live-indicator {
            position: fixed;
            bottom: 2rem;
            right: 2rem;
            background: var(--bg-card);
            border: 1px solid var(--border-color);
            border-radius: 12px;
            padding: 1rem;
            display: flex;
            align-items: center;
            gap: 0.5rem;
            box-shadow: 0 8px 32px rgba(0, 0, 0, 0.3);
        }

        .live-dot {
            width: 12px;
            height: 12px;
            background: var(--danger);
            border-radius: 50%;
            animation: livePulse 1.5s ease-in-out infinite;
        }

        @keyframes livePulse {
            0% {
                box-shadow: 0 0 0 0 rgba(239, 68, 68, 0.7);
            }
            70% {
                box-shadow: 0 0 0 10px rgba(239, 68, 68, 0);
            }
            100% {
                box-shadow: 0 0 0 0 rgba(239, 68, 68, 0);
            }
        }

        /* 로딩 스피너 */
        .loading-spinner {
            width: 40px;
            height: 40px;
            border: 3px solid var(--border-color);
            border-top-color: var(--ajungdang-blue);
            border-radius: 50%;
            animation: spin 1s linear infinite;
            margin: 2rem auto;
        }

        @keyframes spin {
            to { transform: rotate(360deg); }
        }
    </style>
</head>
<body>
    <!-- 헤더 -->
    <header class="header">
        <div class="header-content">
            <div class="logo-section">
                <div class="logo">아</div>
                <h1 class="title">아정당 실시간 상담 모니터링</h1>
            </div>
            <div class="connection-status">
                <div class="status-dot" id="connectionStatus"></div>
                <span id="connectionText">연결됨</span>
            </div>
        </div>
    </header>

    <!-- 메인 컨테이너 -->
    <main class="main-container">
        <!-- 통계 카드 -->
        <div class="stats-grid">
            <div class="stat-card">
                <div class="stat-label">대기 중인 상담</div>
                <div class="stat-value">
                    <span id="waitingCount">0</span>
                    <span class="stat-change" id="waitingChange">+0</span>
                </div>
            </div>
            <div class="stat-card">
                <div class="stat-label">긴급 상담</div>
                <div class="stat-value">
                    <span id="urgentCount">0</span>
                    <span class="stat-change negative" id="urgentChange">0</span>
                </div>
            </div>
            <div class="stat-card">
                <div class="stat-label">평균 대기 시간</div>
                <div class="stat-value">
                    <span id="avgWaitTime">0분</span>
                </div>
            </div>
            <div class="stat-card">
                <div class="stat-label">오늘 총 상담</div>
                <div class="stat-value">
                    <span id="totalToday">0</span>
                </div>
            </div>
        </div>

        <!-- 상담 리스트 -->
        <div class="consultations-section">
            <div class="section-header">
                <h2 class="section-title">
                    <span>📋</span>
                    미답변 상담 목록
                </h2>
                <div class="filter-buttons">
                    <button class="filter-btn active" data-filter="all">전체</button>
                    <button class="filter-btn" data-filter="urgent">긴급</button>
                    <button class="filter-btn" data-filter="recent">최신순</button>
                    <button class="filter-btn" data-filter="oldest">오래된순</button>
                </div>
            </div>
            <div class="consultations-list" id="consultationsList">
                <div class="loading-spinner"></div>
            </div>
        </div>
    </main>

    <!-- 실시간 인디케이터 -->
    <div class="live-indicator">
        <div class="live-dot"></div>
        <span>실시간 모니터링 중</span>
    </div>

    <script>
        // 백엔드 서버 URL 설정 (실제 배포시 변경)
        const API_BASE = window.location.origin; // 같은 도메인 사용
        const WS_URL = `${window.location.protocol === 'https:' ? 'wss:' : 'ws:'}//${window.location.host}/ws`;
        
        class ConsultationMonitor {
            constructor() {
                this.consultations = new Map();
                this.ws = null;
                this.reconnectAttempts = 0;
                this.maxReconnectAttempts = 10;
                this.reconnectDelay = 1000;
                this.stats = {
                    waiting: 0,
                    urgent: 0,
                    avgWaitTime: 0,
                    totalToday: 0
                };
                this.currentFilter = 'all';
                this.init();
            }

            init() {
                this.connectWebSocket();
                this.setupEventListeners();
                this.startPeriodicUpdate();
            }

            connectWebSocket() {
                try {
                    this.ws = new WebSocket(WS_URL);
                    
                    this.ws.onopen = () => {
                        console.log('✅ WebSocket 연결 성공');
                        this.updateConnectionStatus(true);
                        this.reconnectAttempts = 0;
                        this.reconnectDelay = 1000;
                    };

                    this.ws.onmessage = (event) => {
                        const data = JSON.parse(event.data);
                        this.handleWebSocketMessage(data);
                    };

                    this.ws.onclose = () => {
                        console.log('⚠️ WebSocket 연결 끊김');
                        this.updateConnectionStatus(false);
                        this.handleReconnect();
                    };

                    this.ws.onerror = (error) => {
                        console.error('❌ WebSocket 에러:', error);
                        this.updateConnectionStatus(false);
                    };
                    
                } catch (error) {
                    console.error('WebSocket 연결 실패:', error);
                    this.updateConnectionStatus(false);
                    this.handleReconnect();
                }
            }

            handleReconnect() {
                if (this.reconnectAttempts < this.maxReconnectAttempts) {
                    this.reconnectAttempts++;
                    console.log(`재연결 시도 ${this.reconnectAttempts}/${this.maxReconnectAttempts}`);
                    setTimeout(() => {
                        this.connectWebSocket();
                    }, this.reconnectDelay);
                    this.reconnectDelay = Math.min(this.reconnectDelay * 2, 30000); // 최대 30초
                } else {
                    console.error('최대 재연결 시도 횟수 초과');
                    this.showNotification('연결 실패', ' 서버 연결이 끊어졌습니다. 페이지를 새로고침해주세요.');
                }
            }

            generateId() {
                return Date.now().toString(36) + Math.random().toString(36).substr(2, 9);
            }

            handleWebSocketMessage(data) {
                switch (data.type) {
                    case 'initial_data':
                        // 초기 데이터 로드
                        this.consultations.clear();
                        data.consultations.forEach(consultation => {
                            consultation.created_at = new Date(consultation.created_at);
                            this.consultations.set(consultation.id, consultation);
                        });
                        this.stats = data.stats;
                        this.updateDisplay();
                        this.updateStatsDisplay();
                        break;
                        
                    case 'new_consultation':
                        // 새 상담 추가
                        const newConsultation = data.data;
                        newConsultation.created_at = new Date(newConsultation.created_at);
                        this.addConsultation(newConsultation);
                        break;
                        
                    case 'consultation_answered':
                        // 상담 답변 완료
                        this.removeConsultation(data.data.consultation_id);
                        break;
                        
                    case 'stats_update':
                        // 통계 업데이트
                        this.stats = data.stats;
                        this.updateStatsDisplay();
                        break;
                        
                    default:
                        console.log('알 수 없는 메시지 타입:', data.type);
                }
            }

            async loadInitialData() {
                try {
                    // REST API로 초기 데이터 로드 (WebSocket 연결 실패시 대비)
                    const [consultationsRes, statsRes] = await Promise.all([
                        fetch(`${API_BASE}/api/consultations`),
                        fetch(`${API_BASE}/api/stats`)
                    ]);
                    
                    if (consultationsRes.ok && statsRes.ok) {
                        const consultations = await consultationsRes.json();
                        const stats = await statsRes.json();
                        
                        this.consultations.clear();
                        consultations.forEach(consultation => {
                            consultation.created_at = new Date(consultation.created_at);
                            this.consultations.set(consultation.id, consultation);
                        });
                        
                        this.stats = stats;
                        this.updateDisplay();
                        this.updateStatsDisplay();
                    }
                } catch (error) {
                    console.error('초기 데이터 로드 실패:', error);
                }
            }

            addConsultation(consultation) {
                if (!consultation.created_at) {
                    consultation.created_at = new Date();
                }
                this.consultations.set(consultation.id, consultation);
                this.updateDisplay();
                this.updateStats();
                
                // 알림 표시
                this.showNotification('새 상담', `${consultation.customer || 'Unknown'}님의 상담이 접수되었습니다.`);
                
                // 긴급 상담인 경우 추가 알림
                if (consultation.status === 'urgent' || consultation.wait_time_minutes > 15) {
                    this.showNotification('⚠️ 긴급 상담', `${consultation.customer || 'Unknown'}님이 ${consultation.wait_time_minutes}분째 대기 중입니다.`);
                }
            }

            removeConsultation(consultationId) {
                if (this.consultations.has(consultationId)) {
                    const consultation = this.consultations.get(consultationId);
                    this.consultations.delete(consultationId);
                    this.updateDisplay();
                    this.updateStats();
                    
                    // 상담 완료 알림
                    this.showNotification('상담 완료', `${consultation.customer || 'Unknown'}님의 상담이 처리되었습니다.`);
                }
            }

            updateDisplay() {
                const listElement = document.getElementById('consultationsList');
                const consultationsArray = Array.from(this.consultations.values());
                
                // 필터링
                let filtered = consultationsArray;
                switch (this.currentFilter) {
                    case 'urgent':
                        filtered = consultationsArray.filter(c => c.status === 'urgent' || c.wait_time_minutes > 15);
                        break;
                    case 'recent':
                        filtered = consultationsArray.sort((a, b) => new Date(b.created_at) - new Date(a.created_at));
                        break;
                    case 'oldest':
                        filtered = consultationsArray.sort((a, b) => new Date(a.created_at) - new Date(b.created_at));
                        break;
                }

                if (filtered.length === 0) {
                    listElement.innerHTML = `
                        <div class="empty-state">
                            <div class="empty-icon">🎉</div>
                            <p>모든 상담이 처리되었습니다!</p>
                        </div>
                    `;
                    return;
                }

                listElement.innerHTML = filtered.map(consultation => {
                    const createdAt = new Date(consultation.created_at);
                    const waitMinutes = consultation.wait_time_minutes || Math.floor((Date.now() - createdAt) / 60000);
                    const isNew = (Date.now() - createdAt) < 60000;
                    const isUrgent = consultation.status === 'urgent' || waitMinutes > 15;
                    
                    return `
                        <div class="consultation-item ${isNew ? 'new' : ''}" data-id="${consultation.id}">
                            <div class="consultation-info">
                                <div class="consultation-id">ID: ${consultation.id}</div>
                                <div class="consultation-customer">${consultation.customer || 'Unknown'}</div>
                                <div class="consultation-meta">
                                    <span class="meta-item">
                                        🕒 ${this.formatTime(createdAt)}
                                    </span>
                                    <span class="meta-item">
                                        ⏱️ 대기 ${waitMinutes}분
                                    </span>
                                </div>
                            </div>
                            <div class="consultation-status">
                                <span class="status-badge ${isUrgent ? 'urgent' : 'waiting'}">
                                    ${isUrgent ? '긴급' : '대기중'}
                                </span>
                                <button class="action-btn" onclick="monitor.handleConsultation('${consultation.id}')">
                                    상담 시작
                                </button>
                            </div>
                        </div>
                    `;
                }).join('');
            }

            updateStats() {
                const consultationsArray = Array.from(this.consultations.values());
                
                this.stats.waiting = consultationsArray.filter(c => c.status === 'waiting').length;
                this.stats.urgent = consultationsArray.filter(c => c.status === 'urgent' || 
                    Math.floor((Date.now() - c.createdAt) / 60000) > 15).length;
                
                const totalWaitTime = consultationsArray.reduce((sum, c) => 
                    sum + Math.floor((Date.now() - c.createdAt) / 60000), 0);
                this.stats.avgWaitTime = consultationsArray.length > 0 ? 
                    Math.round(totalWaitTime / consultationsArray.length) : 0;
                
                this.stats.totalToday = consultationsArray.length + Math.floor(Math.random() * 50);
                
                this.updateStatsDisplay();
            }

            updateStatsDisplay() {
                document.getElementById('waitingCount').textContent = this.stats.waiting;
                document.getElementById('urgentCount').textContent = this.stats.urgent;
                document.getElementById('avgWaitTime').textContent = `${this.stats.avgWaitTime}분`;
                document.getElementById('totalToday').textContent = this.stats.totalToday;
                
                // 변화량 표시
                const waitingChange = document.getElementById('waitingChange');
                waitingChange.textContent = this.stats.waiting > 0 ? `+${this.stats.waiting}` : '0';
                waitingChange.className = this.stats.waiting > 5 ? 'stat-change negative' : 'stat-change';
                
                document.getElementById('urgentChange').textContent = this.stats.urgent;
            }

            updateConnectionStatus(connected) {
                const statusDot = document.getElementById('connectionStatus');
                const statusText = document.getElementById('connectionText');
                
                if (connected) {
                    statusDot.className = 'status-dot';
                    statusText.textContent = '연결됨';
                } else {
                    statusDot.className = 'status-dot disconnected';
                    statusText.textContent = '연결 끊김';
                }
            }

            setupEventListeners() {
                // 필터 버튼
                document.querySelectorAll('.filter-btn').forEach(btn => {
                    btn.addEventListener('click', (e) => {
                        document.querySelectorAll('.filter-btn').forEach(b => b.classList.remove('active'));
                        e.target.classList.add('active');
                        this.currentFilter = e.target.dataset.filter;
                        this.updateDisplay();
                    });
                });
            }

            startPeriodicUpdate() {
                // 1분마다 데이터 새로고침 (WebSocket 백업)
                setInterval(() => {
                    if (!this.ws || this.ws.readyState !== WebSocket.OPEN) {
                        this.loadInitialData();
                    }
                    // 대기 시간 실시간 업데이트
                    this.updateDisplay();
                }, 60000);
            }

            loadInitialData() {
                // WebSocket 연결 후 자동으로 초기 데이터를 받으므로
                // 연결 실패시에만 REST API 호출
                if (!this.ws || this.ws.readyState !== WebSocket.OPEN) {
                    setTimeout(() => this.loadInitialData(), 2000);
                }
            }

            async handleConsultation(consultationId) {
                console.log('상담 시작:', consultationId);
                
                try {
                    // API 호출로 상담을 답변 완료로 표시
                    const response = await fetch(`${API_BASE}/api/consultations/${consultationId}/answer`, {
                        method: 'POST',
                        headers: {
                            'Content-Type': 'application/json'
                        }
                    });
                    
                    if (response.ok) {
                        // 채널톡으로 이동 (실제 채널톡 URL로 변경 필요)
                        window.open(`https://desk.channel.io/#/channels/YOUR_CHANNEL_ID/user_chats/${consultationId}`, '_blank');
                        this.removeConsultation(consultationId);
                    } else {
                        console.error('상담 처리 실패');
                        this.showNotification('오류', '상담 처리에 실패했습니다.');
                    }
                } catch (error) {
                    console.error('API 호출 실패:', error);
                    this.showNotification('오류', '서버 연결에 실패했습니다.');
                }
            }

            showNotification(title, message) {
                if ('Notification' in window && Notification.permission === 'granted') {
                    new Notification(title, {
                        body: message,
                        icon: '/favicon.ico',
                        badge: '/favicon.ico'
                    });
                }
            }

            formatTime(date) {
                return new Date(date).toLocaleTimeString('ko-KR', { 
                    hour: '2-digit', 
                    minute: '2-digit' 
                });
            }
        }

        // 알림 권한 요청
        if ('Notification' in window && Notification.permission === 'default') {
            Notification.requestPermission();
        }

        // 모니터 인스턴스 생성
        const monitor = new ConsultationMonitor();
    </script>
</body>
</html>
