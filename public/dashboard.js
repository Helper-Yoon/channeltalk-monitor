class DashboardManager {
  constructor() {
    this.socket = null;
    this.consultations = new Map();
    this.activeTeam = 'all';
    this.connectionRetries = 0;
  }

  init() {
    this.connectWebSocket();
    this.setupEventListeners();
    this.updateClock();
    setInterval(() => this.updateWaitTimes(), 30000); // 30초마다 대기시간 업데이트
  }

  connectWebSocket() {
    this.socket = io({
      transports: ['websocket', 'polling'],
      reconnection: true,
      reconnectionAttempts: 10,
      reconnectionDelay: 1000
    }); 

    this.socket.on('connect', () => {
      console.log('Connected to server');
      this.updateConnectionStatus('연결됨', '#10b981');
      this.socket.emit('join:dashboard');
    });

    this.socket.on('disconnect', () => {
      this.updateConnectionStatus('연결 끊김', '#ef4444');
    }); 

    this.socket.on('dashboard:init', (data) => {
      this.handleInitialData(data);
    });

    this.socket.on('dashboard:update', (data) => {
      this.handleUpdate(data);
    });

    this.socket.on('consultation:new', (consultation) => {
      this.addConsultation(consultation);
    });

    this.socket.on('consultation:updated', (consultation) => {
      this.updateConsultation(consultation);
    });
  }

  updateConnectionStatus(text, color) {
    document.getElementById('connection-text').textContent = text;
    document.querySelector('.status-indicator').style.background = color;
  }

  handleInitialData(consultations) {
    this.consultations.clear();
    consultations.forEach(c => {
      this.consultations.set(c.id, c);
    });
    this.renderConsultations();
  }

  handleUpdate(consultations) {
    this.consultations.clear();
    consultations.forEach(c => {
      this.consultations.set(c.id, c);
    });
    this.renderConsultations();
  }

  addConsultation(consultation) {
    this.consultations.set(consultation.id, consultation);
    this.renderConsultations();
  }

  updateConsultation(consultation) {
    if (this.consultations.has(consultation.id)) {
      const existing = this.consultations.get(consultation.id);
      this.consultations.set(consultation.id, { ...existing, ...consultation });
      this.renderConsultations();
    }
  }

  renderConsultations() {
    const grid = document.getElementById('consultations-grid');
    const filtered = this.filterConsultations();
    
    grid.innerHTML = '';
    
    if (filtered.length === 0) {
      grid.innerHTML = '<div class="no-data">현재 미답변 상담이 없습니다</div>';
      document.getElementById('total-count').textContent = '총 0건';
      return;
    }
    
    filtered.forEach(consultation => {
      const card = this.createConsultationCard(consultation);
      grid.appendChild(card);
    });
    
    document.getElementById('total-count').textContent = `총 ${filtered.length}건`;
  }

  filterConsultations() {
    const consultations = Array.from(this.consultations.values());
    
    if (this.activeTeam === 'all') {
      return consultations;
    }
    
    return consultations.filter(c => c.team === this.activeTeam);
  }

  createConsultationCard(consultation) {
    const card = document.createElement('div');
    card.className = 'consultation-card';
    card.dataset.consultationId = consultation.id;
    
    const waitTimeClass = this.getWaitTimeClass(consultation.waitTime);
    const waitTimeText = this.formatWaitTime(consultation.waitTime);
    
    card.innerHTML = `
      <div class="consultation-header">
        <div class="customer-name">${consultation.customerName || '익명'}</div>
        <span class="wait-time-indicator ${waitTimeClass}">${waitTimeText}</span>
      </div>
      <div class="consultation-details">
        <p><span class="detail-label">팀:</span>${consultation.team || '미배정'}</p>
        <p><span class="detail-label">담당자:</span>${consultation.counselor || '대기중'}</p>
        <p><span class="detail-label">접수시간:</span>${this.formatTime(consultation.createdAt)}</p>
      </div>
      ${consultation.customerMessage ? `
        <div class="customer-message">
          ${this.escapeHtml(consultation.customerMessage)}
        </div>
      ` : ''}
    `;
    
    // 더블클릭 이벤트
    card.addEventListener('dblclick', () => {
      window.open(consultation.chatUrl, '_blank');
    });
    
    return card;
  }

  getWaitTimeClass(minutes) {
    if (minutes < 3) return 'wait-time-green';
    if (minutes <= 4) return 'wait-time-blue';
    if (minutes <= 6) return 'wait-time-yellow';
    if (minutes <= 10) return 'wait-time-orange';
    return 'wait-time-red';
  }

  formatWaitTime(minutes) {
    if (minutes < 60) return `${minutes}분`;
    const hours = Math.floor(minutes / 60);
    const mins = minutes % 60;
    return `${hours}시간 ${mins}분`;
  }

  formatTime(timestamp) {
    const date = new Date(parseInt(timestamp));
    return date.toLocaleTimeString('ko-KR', { 
      hour: '2-digit', 
      minute: '2-digit' 
    });
  }

  escapeHtml(text) {
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
  }

  setupEventListeners() {
    // 팀 필터 버튼
    document.querySelectorAll('.team-filter-btn').forEach(btn => {
      btn.addEventListener('click', (e) => {
        document.querySelectorAll('.team-filter-btn').forEach(b => 
          b.classList.remove('active')
        );
        e.target.classList.add('active');
        this.activeTeam = e.target.dataset.team;
        this.renderConsultations();
      });
    });
  }

  updateClock() {
    const updateTime = () => {
      const now = new Date();
      document.getElementById('current-time').textContent = 
        now.toLocaleTimeString('ko-KR');
    };
    
    updateTime();
    setInterval(updateTime, 1000);
  }

  updateWaitTimes() {
    // 대기시간 재계산 및 UI 업데이트
    this.consultations.forEach((consultation, id) => {
      const waitTime = Math.floor((Date.now() - consultation.frontUpdatedAt) / 60000);
      consultation.waitTime = waitTime;
    });
    this.renderConsultations();
  }
}

// 대시보드 초기화
document.addEventListener('DOMContentLoaded', () => {
  const dashboard = new DashboardManager();
  dashboard.init();
});
