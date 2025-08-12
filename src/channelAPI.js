// src/channelAPI.js - 오류 수정된 완전한 최종 버전
import fetch from 'node-fetch';

export class ChannelTalkService {
  constructor(redisClient, io) {
    this.redis = redisClient;
    this.io = io;
    this.apiKey = process.env.CHANNEL_API_KEY || '688a26176fcb19aebf8b';
    this.apiSecret = process.env.CHANNEL_API_SECRET || 'a0db6c38b95c8ec4d9bb46e7c653b3e2';
    this.baseURL = 'https://api.channel.io/open/v5';
    this.managers = {}; // 매니저 정보 캐시
    
    // 팀별 담당자 매핑 - 로그에서 확인된 실제 이름들 포함
    this.teamMembers = {
      'SNS 1팀': ['이종민', '정주연', '이혜영', '김국현', '정다혜', '조시현', '김시윤'],
      'SNS 2팀': ['윤도우리', '신혜서', '김상아', '박은진', '오민환', '서정국'],
      'SNS 3팀': ['김진후', '김시진', '권재현', '김지원', '최호익', '김진협', '박해영'],
      'SNS 4팀': ['이민주', '전지윤', '전미란', '김채영', '김영진', '강헌준'],
      '의정부 SNS팀': ['차정환', '최수능', '구본영', '서민국', '오민경', '김범주', '동수진', '성일훈']
    };
  }

  async makeRequest(endpoint, options = {}) {
    const url = `${this.baseURL}${endpoint}`;
    const headers = {
      'x-access-key': this.apiKey,
      'x-access-secret': this.apiSecret,
      'accept': 'application/json',
      ...options.headers
    };

    try {
      const response = await fetch(url, { 
        ...options, 
        headers,
        timeout: 10000 
      });
      
      if (!response.ok) {
        const errorText = await response.text();
        console.error(`API Error: ${response.status} - ${errorText}`);
        throw new Error(`HTTP ${response.status}`);
      }
      
      return await response.json();
    } catch (error) {
      console.error('Channel API request failed:', error);
      throw error;
    }
  }

  async loadManagers() {
    try {
      const data = await this.makeRequest('/managers');
      this.managers = {};
      if (data.managers) {
        data.managers.forEach(manager => {
          this.managers[manager.id] = manager;
          console.log(`Manager loaded: ${manager.name || manager.displayName || manager.email} (ID: ${manager.id})`);
        });
      }
      console.log(`Loaded ${Object.keys(this.managers).length} managers`);
    } catch (error) {
      console.error('Failed to load managers:', error);
    }
  }

  // 담당자 이름으로 팀 찾기
  findTeamByName(name) {
    if (!name || name === '미배정') return '없음';
    
    // 이름 정제
    const fullName = String(name);
    const cleanName = fullName.replace(/[^\uAC00-\uD7AF\u1100-\u11FF\u3130-\u318F]/g, '');
    
    console.log(`Finding team for: ${fullName} (cleaned: ${cleanName})`);
    
    // 정확한 이름 매칭
    for (const [team, members] of Object.entries(this.teamMembers)) {
      // 완전 일치 확인
      if (members.includes(cleanName) || members.includes(fullName)) {
        console.log(`Exact match: ${fullName} -> ${team}`);
        return team;
      }
      // 부분 일치 확인
      if (members.some(member => {
        const matched = cleanName.includes(member) || 
                       member.includes(cleanName) || 
                       fullName.includes(member) || 
                       member.includes(fullName.split(' ')[0]) ||
                       member.includes(fullName.split('_')[0]) ||
                       member.includes(fullName.split('-')[0]);
        if (matched) {
          console.log(`Partial match: ${fullName} matches ${member} -> ${team}`);
        }
        return matched;
      })) {
        return team;
      }
    }
    
    console.log(`No team found for: ${name}`);
    return '없음';
  }

  async syncOpenChats() {
    try {
      console.log('=== Starting sync ===');
      
      // 매니저 정보 먼저 로드
      await this.loadManagers();
      
      // 열린 상담 조회 - sortOrder를 desc로 수정
      const data = await this.makeRequest('/user-chats?state=opened&limit=200&sortOrder=desc');
      const userChats = data.userChats || [];
      
      console.log(`Found ${userChats.length} open chats`);
      
      // 미답변 상담 찾기
      const unansweredChats = [];
      
      for (const chat of userChats) {
        try {
          // 채팅의 전체 정보 가져오기
          const chatDetail = await this.makeRequest(`/user-chats/${chat.id}`);
          const fullChat = chatDetail.userChat || chat;
          
          console.log(`Processing chat ${chat.id}:`);
          console.log('- User info:', JSON.stringify(fullChat.user));
          console.log('- Assignee:', JSON.stringify(fullChat.assignee));
          console.log('- AssigneeId:', fullChat.assigneeId);
          
          // 마지막 메시지 확인 - sortOrder를 desc로 수정
          const messagesData = await this.makeRequest(`/user-chats/${chat.id}/messages?sortOrder=desc&limit=20`);
          const messages = messagesData.messages || [];
          
          // 마지막 고객 메시지 찾기
          const lastCustomerMessage = messages.find(m => m.personType === 'user');
          const lastManagerMessage = messages.find(m => m.personType === 'manager');
          
          // 고객이 마지막으로 메시지를 보낸 경우
          if (lastCustomerMessage && (!lastManagerMessage || lastCustomerMessage.createdAt > lastManagerMessage.createdAt)) {
            
            // 담당자 정보 가져오기
            let counselorName = '미배정';
            let teamName = '없음';
            
            // assignee 객체가 직접 있는 경우 (우선순위 1)
            if (fullChat.assignee) {
              counselorName = fullChat.assignee.name || 
                            fullChat.assignee.displayName || 
                            fullChat.assignee.email?.split('@')[0] || 
                            '미배정';
              teamName = this.findTeamByName(counselorName);
              console.log(`Direct assignee: ${counselorName} -> Team: ${teamName}`);
            }
            // assigneeId로 매니저 찾기 (우선순위 2)
            else if (fullChat.assigneeId && this.managers[fullChat.assigneeId]) {
              const assignee = this.managers[fullChat.assigneeId];
              counselorName = assignee.name || 
                            assignee.displayName || 
                            assignee.email?.split('@')[0] || 
                            '미배정';
              teamName = this.findTeamByName(counselorName);
              console.log(`Manager lookup: ${counselorName} -> Team: ${teamName}`);
            }
            
            // 고객 정보 - 모든 가능한 필드 확인
            const customerName = fullChat.user?.name || 
                               fullChat.user?.profile?.name || 
                               fullChat.user?.username || 
                               fullChat.name ||
                               fullChat.userName ||
                               fullChat.user?.displayName ||
                               fullChat.user?.email?.split('@')[0] || 
                               fullChat.user?.phoneNumber ||
                               fullChat.user?.id ||
                               fullChat.userId ||
                               '익명';
            
            console.log(`Customer name found: ${customerName}`);
            
            const consultationData = {
              id: String(chat.id),
              customerName: String(customerName),
              customerMessage: String(lastCustomerMessage.plainText || lastCustomerMessage.message || ''),
              team: teamName,
              counselor: counselorName,
              waitTime: String(this.calculateWaitTime(lastCustomerMessage.createdAt)),
              createdAt: String(chat.createdAt),
              frontUpdatedAt: String(lastCustomerMessage.createdAt),
              chatUrl: `https://desk.channel.io/#/channels/197228/user_chats/${chat.id}`
            };
            
            unansweredChats.push(consultationData);
            
            // Redis에 저장
            const redisData = Object.entries(consultationData)
              .map(([key, value]) => [key, String(value)])
              .flat();
            
            await this.redis.hSet(`consultation:${chat.id}`, redisData);
            await this.redis.zAdd('consultations:waiting', {
              score: lastCustomerMessage.createdAt,
              value: String(chat.id)
            });
            await this.redis.expire(`consultation:${chat.id}`, 86400);
          }
        } catch (error) {
          console.error(`Error processing chat ${chat.id}:`, error.message);
        }
      }
      
      console.log(`=== Sync complete: ${unansweredChats.length} unanswered chats ===`);
      
      // 시간 내림차순, 같으면 고객명 내림차순 정렬 - null 체크 추가
      unansweredChats.sort((a, b) => {
        const waitTimeDiff = parseInt(b.waitTime) - parseInt(a.waitTime);
        if (waitTimeDiff !== 0) return waitTimeDiff;
        
        // customerName이 없을 경우 처리
        const nameA = a.customerName || '익명';
        const nameB = b.customerName || '익명';
        return nameB.localeCompare(nameA, 'ko');
      });
      
      // 대시보드 업데이트
      this.io.to('dashboard').emit('dashboard:update', unansweredChats);
      
    } catch (error) {
      console.error('Sync error:', error);
    }
  }

  calculateWaitTime(timestamp) {
    const now = Date.now();
    const created = parseInt(timestamp) || 0;
    const waitTimeMs = now - created;
    return Math.floor(waitTimeMs / 1000 / 60); // 분 단위
  }

  async getUnansweredConsultations() {
    try {
      const consultationIds = await this.redis.zRange('consultations:waiting', 0, -1, { REV: true });
      const consultations = [];
      
      for (const id of consultationIds) {
        const data = await this.redis.hGetAll(`consultation:${id}`);
        if (data && Object.keys(data).length > 0) {
          data.waitTime = String(this.calculateWaitTime(parseInt(data.frontUpdatedAt)));
          consultations.push(data);
        }
      }
      
      // 시간 내림차순, 같으면 고객명 내림차순 정렬 - null 체크 추가
      consultations.sort((a, b) => {
        const waitTimeDiff = parseInt(b.waitTime) - parseInt(a.waitTime);
        if (waitTimeDiff !== 0) return waitTimeDiff;
        
        // customerName이 없을 경우 처리
        const nameA = a.customerName || '익명';
        const nameB = b.customerName || '익명';
        return nameB.localeCompare(nameA, 'ko');
      });
      
      return consultations;
    } catch (error) {
      console.error('Error getting consultations:', error);
      return [];
    }
  }
}
