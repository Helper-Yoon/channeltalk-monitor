// src/channelAPI.js - 타입 에러 수정 버전
import fetch from 'node-fetch';

export class ChannelTalkService {
  constructor(redisClient, io) {
    this.redis = redisClient;
    this.io = io;
    this.apiKey = process.env.CHANNEL_API_KEY || '688a26176fcb19aebf8b';
    this.apiSecret = process.env.CHANNEL_API_SECRET || 'a0db6c38b95c8ec4d9bb46e7c653b3e2';
    this.baseURL = 'https://api.channel.io/open/v5';
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

  async syncOpenChats() {
    try {
      console.log('Syncing open chats...');
      
      // 열린 상담 조회
      const data = await this.makeRequest('/user-chats?state=opened&limit=200');
      const userChats = data.userChats || [];
      
      console.log(`Found ${userChats.length} open chats`);
      
      // 매니저 정보 조회
      const managersData = await this.makeRequest('/managers');
      const managers = {};
      if (managersData.managers) {
        managersData.managers.forEach(m => {
          managers[m.id] = m;
        });
      }
      
      // 미답변 상담 찾기
      const unansweredChats = [];
      
      for (const chat of userChats) {
        try {
          // 마지막 메시지 확인
          const messagesData = await this.makeRequest(`/user-chats/${chat.id}/messages?limit=1`);
          const lastMessage = messagesData.messages?.[0];
          
          // 고객이 마지막으로 메시지를 보낸 경우
          if (lastMessage && lastMessage.personType === 'user') {
            const assignee = chat.assignee ? managers[chat.assignee] : null;
            
            const consultationData = {
              id: String(chat.id),  // 문자열로 변환
              customerName: String(chat.user?.name || '익명'),
              customerMessage: String(lastMessage.plainText || ''),
              team: this.assignTeam(assignee),
              counselor: String(assignee?.name || '대기중'),
              waitTime: String(this.calculateWaitTime(lastMessage.createdAt)),
              createdAt: String(chat.createdAt),
              frontUpdatedAt: String(lastMessage.createdAt),
              chatUrl: `https://desk.channel.io/#/channels/197228/user_chats/${chat.id}`
            };
            
            unansweredChats.push(consultationData);
            
            // Redis에 저장 - 모든 값을 문자열로 변환
            const redisData = Object.entries(consultationData)
              .map(([key, value]) => [key, String(value)])
              .flat();
            
            await this.redis.hSet(`consultation:${chat.id}`, redisData);
            await this.redis.zAdd('consultations:waiting', {
              score: lastMessage.createdAt,
              value: String(chat.id)
            });
            await this.redis.expire(`consultation:${chat.id}`, 86400);
          }
        } catch (error) {
          console.error(`Error processing chat ${chat.id}:`, error);
        }
      }
      
      console.log(`Found ${unansweredChats.length} unanswered consultations`);
      
      // 대시보드 업데이트
      this.io.to('dashboard').emit('dashboard:update', unansweredChats);
      
    } catch (error) {
      console.error('Sync error:', error);
    }
  }

  assignTeam(assignee) {
    if (!assignee) return '미배정';
    
    const name = String(assignee.name || '');
    const team = String(assignee.team || '');
    const dept = String(assignee.department || '');
    
    // 팀 매핑 로직 - 이름, 팀, 부서에서 찾기
    const fullInfo = `${name} ${team} ${dept}`.toLowerCase();
    
    if (fullInfo.includes('sns 1팀') || fullInfo.includes('sns1')) return 'SNS 1팀';
    if (fullInfo.includes('sns 2팀') || fullInfo.includes('sns2')) return 'SNS 2팀';
    if (fullInfo.includes('sns 3팀') || fullInfo.includes('sns3')) return 'SNS 3팀';
    if (fullInfo.includes('sns 4팀') || fullInfo.includes('sns4')) return 'SNS 4팀';
    if (fullInfo.includes('의정부')) return '의정부 SNS팀';
    
    // 기본값
    return 'SNS 1팀';
  }

  calculateWaitTime(timestamp) {
    const now = Date.now();
    const created = parseInt(timestamp) || 0;
    const waitTimeMs = now - created;
    return Math.floor(waitTimeMs / 1000 / 60); // 분 단위
  }

  async getUnansweredConsultations() {
    try {
      const consultationIds = await this.redis.zRange('consultations:waiting', 0, -1);
      const consultations = [];
      
      for (const id of consultationIds) {
        const data = await this.redis.hGetAll(`consultation:${id}`);
        if (data && Object.keys(data).length > 0) {
          // 모든 값이 문자열이므로 필요한 것만 숫자로 변환
          data.waitTime = String(this.calculateWaitTime(parseInt(data.frontUpdatedAt)));
          consultations.push(data);
        }
      }
      
      // 대기시간 기준 정렬 (긴 시간이 먼저)
      return consultations.sort((a, b) => parseInt(b.waitTime) - parseInt(a.waitTime));
    } catch (error) {
      console.error('Error getting consultations:', error);
      return [];
    }
  }
}
