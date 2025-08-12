// src/channelAPI.js - 완전 수정 버전

// 1. 매니저 정보 로딩 개선 (loadManagers 메서드)
async loadManagers() {
  try {
    const data = await this.makeRequest('/managers');
    this.managers = {};
    if (data.managers) {
      data.managers.forEach(manager => {
        this.managers[manager.id] = manager;
        // 매니저 이름과 displayName 모두 로그
        console.log(`Manager loaded: ${manager.name || manager.displayName} (ID: ${manager.id})`);
      });
    }
    console.log(`Loaded ${Object.keys(this.managers).length} managers`);
  } catch (error) {
    console.error('Failed to load managers:', error);
  }
}

// 2. 팀 찾기 로직 개선 (findTeamByName 메서드)
findTeamByName(name) {
  if (!name || name === '미배정') return '없음';
  
  // 이름 정제 - 괄호 안 내용도 확인
  const fullName = String(name);
  const cleanName = fullName.replace(/[^\uAC00-\uD7AF\u1100-\u11FF\u3130-\u318F]/g, '');
  
  // 정확한 이름 매칭
  for (const [team, members] of Object.entries(this.teamMembers)) {
    // 완전 일치 확인
    if (members.includes(cleanName)) {
      console.log(`Exact match: ${cleanName} -> ${team}`);
      return team;
    }
    // 부분 일치 확인
    if (members.some(member => {
      return cleanName.includes(member) || member.includes(cleanName) || 
             fullName.includes(member) || member.includes(fullName.split(' ')[0]);
    })) {
      console.log(`Partial match: ${name} -> ${team}`);
      return team;
    }
  }
  
  console.log(`No team found for: ${name}`);
  return '없음';
}

// 3. syncOpenChats 메서드에서 고객 정보 개선
// 고객 정보 가져오기 부분 수정
const customerName = fullChat.user?.name || 
                   fullChat.user?.profile?.name || 
                   fullChat.user?.username || 
                   fullChat.name ||  // 추가
                   fullChat.userName ||  // 추가
                   fullChat.user?.email?.split('@')[0] || 
                   fullChat.user?.phoneNumber ||
                   fullChat.user?.id ||  // 추가
                   '익명';

// 담당자 정보 개선
let counselorName = '미배정';
let teamName = '없음';

if (fullChat.assigneeId && this.managers[fullChat.assigneeId]) {
  const assignee = this.managers[fullChat.assigneeId];
  counselorName = assignee.name || assignee.displayName || '미배정';
  teamName = this.findTeamByName(counselorName);
  console.log(`Chat ${chat.id}: Assignee ${counselorName} -> Team ${teamName}`);
} else if (fullChat.assignee) {
  // assignee 객체가 직접 있는 경우
  counselorName = fullChat.assignee.name || fullChat.assignee.displayName || '미배정';
  teamName = this.findTeamByName(counselorName);
  console.log(`Chat ${chat.id}: Direct assignee ${counselorName} -> Team ${teamName}`);
}
