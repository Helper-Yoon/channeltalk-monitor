// src/channelAPI.js - 수정 부분

async loadManagers() {
  // 매니저 정보는 캐시되어 있으면 재사용
  if (Object.keys(this.managers).length > 0) {
    console.log('Using cached managers');
    return;
  }
  
  try {
    this.managers = {};
    let hasMore = true;
    let offset = 0;
    let totalManagers = 0;
    
    // 페이지네이션으로 모든 매니저 가져오기
    while (hasMore) {
      const data = await this.makeRequest(`/managers?limit=100&offset=${offset}`);
      
      if (data.managers && data.managers.length > 0) {
        data.managers.forEach(manager => {
          this.managers[manager.id] = manager;
          totalManagers++;
        });
        
        // 다음 페이지 확인
        if (data.managers.length < 100) {
          hasMore = false;
        } else {
          offset += 100;
          await this.delay(500); // Rate limit 방지
        }
      } else {
        hasMore = false;
      }
    }
    
    console.log(`Loaded ${totalManagers} managers (in ${Object.keys(this.managers).length} unique IDs)`);
    
    // 김범주님 확인
    const kimManager = Object.values(this.managers).find(m => 
      m.name === '김범주' || m.displayName === '김범주'
    );
    if (kimManager) {
      console.log('김범주 매니저 확인됨:', kimManager);
    } else {
      console.log('⚠️ 김범주 매니저를 찾을 수 없음');
    }
    
  } catch (error) {
    console.error('Failed to load managers:', error);
  }
}

async syncOpenChats() {
  try {
    console.log('=== Starting sync ===');
    this.apiCallCount = 0;
    
    // 최소 동기화 간격 설정 (60초)
    const now = Date.now();
    if (now - this.lastSyncTime < 60000) {
      console.log('Skipping sync - too soon');
      return;
    }
    this.lastSyncTime = now;
    
    // 매니저 정보 로드 (캐시 강제 갱신 옵션 추가)
    if (Object.keys(this.managers).length === 0 || now - this.lastManagerLoad > 3600000) {
      this.managers = {}; // 캐시 초기화
      this.lastManagerLoad = now;
      await this.loadManagers();
    } else {
      console.log('Using cached managers');
    }
    
    // ... 나머지 코드는 동일 ...
    
    // 담당자 정보 처리 부분 수정
    if (fullChat.assigneeId && this.managers[fullChat.assigneeId]) {
      const assignee = this.managers[fullChat.assigneeId];
      counselorName = assignee.name || assignee.displayName || '미배정';
      teamName = this.findTeamByName(counselorName);
      
      // 디버깅: 김범주님 상담인 경우
      if (counselorName.includes('김범주')) {
        console.log(`김범주 상담 발견 - Chat ID: ${chat.id}, Team: ${teamName}`);
      }
    } else if (fullChat.assigneeId) {
      // assigneeId는 있는데 매니저 목록에 없는 경우
      console.log(`⚠️ Manager not found for assigneeId: ${fullChat.assigneeId}`);
      counselorName = '확인필요';
      teamName = '확인필요';
    }
    
    // ... 나머지 코드는 동일 ...
  }
}

// 생성자에 추가
constructor(redisClient, io) {
  // ... 기존 코드 ...
  this.lastManagerLoad = 0; // 매니저 마지막 로드 시간
  
  // 팀별 담당자 매핑 - 김범주님 확인
  this.teamMembers = {
    'SNS 1팀': ['이종민', '정주연', '이혜영', '김국현', '정다혜', '조시현', '김시윤'],
    'SNS 2팀': ['윤도우리', '신혜서', '김상아', '박은진', '오민환', '서정국'],
    'SNS 3팀': ['김진후', '김시진', '권재현', '김지원', '최호익', '김진협', '박해영'],
    'SNS 4팀': ['이민주', '전지윤', '전미란', '김채영', '김영진', '강헌준'],
    '의정부 SNS팀': ['차정환', '최수능', '구본영', '서민국', '오민경', '김범주', '동수진', '성일훈', '손진우'],
    '스킬팀': [], // 스킬팀 추가 (필요시 여기에 담당자 추가)
    '기타': ['채주은', '강형욱']
  };
}
