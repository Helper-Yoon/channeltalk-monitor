// src/channelAPI.js - 최적화 버전 with quickSync
import fetch from 'node-fetch';

export class ChannelTalkService {
  constructor(redisClient, io) {
    this.redis = redisClient;
    this.io = io;
    this.apiKey = process.env.CHANNEL_API_KEY || '688a26176fcb19aebf8b';
    this.apiSecret = process.env.CHANNEL_API_SECRET || 'a0db6c38b95c8ec4d9bb46e7c653b3e2';
    this.baseURL = 'https://api.channel.io/open/v5';
    this.managers = {}; // 매니저 정보 캐시
    this.processedChats = new Set(); // 이미 처리한 채팅 ID 저장
    this.apiCallCount = 0; // API 호출 횟수 추적
    this.lastSyncTime = 0; // 마지막 동기화 시간
    this.lastManagerLoad = 0; // 매니저 마지막 로드 시간
    this.lastFullScan = 0; // 마지막 전체 스캔 시간
    
    // 팀별 담당자 매핑
    this.teamMembers = {
      'SNS 1팀': ['이종민', '정주연', '이혜영', '김국현', '정다혜', '조시현', '김시윤'],
      'SNS 2팀': ['윤도우리', '신혜서', '김상아', '박은진', '오민환', '서정국'],
      'SNS 3팀': ['김진후', '김시진', '권재현', '김지원', '최호익', '김진협', '박해영'],
      'SNS 4팀': ['이민주', '전지윤', '전미란', '김채영', '김영진', '강헌준'],
      '의정부 SNS팀': ['차정환', '최수능', '구본영', '서민국', '오민경', '김범주', '동수진', '성일훈', '손진우'],
      '기타': ['채주은', '강형욱']
    };
  }

  // API 호출에 딜레이 추가 (Rate Limit 방지)
  async delay(ms) {
    return new Promise(resolve => setTimeout(resolve, ms));
  }

  async makeRequest(endpoint, options = {}) {
    // Rate limit 방지: API 호출 간 딜레이
    await this.delay(300); // 300ms 딜레이
    
    this.apiCallCount++;
    
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
      
      if (response.status === 429) {
        // Rate limit 도달시 10초 대기 후 재시도
        console.warn('Rate limit reached, waiting 10 seconds...');
        await this.delay(10000);
        return this.makeRequest(endpoint, options);
      }
      
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
      this.managers = {};
      let offset = 0;
      let totalManagers = 0;
      let hasMore = true;
      
      console.log('Loading ALL managers from Channel Talk...');
      
      while (hasMore) {
        try {
          const endpoint = `/managers?limit=500&offset=${offset}`;
          const data = await this.makeRequest(endpoint);
          
          if (data.managers && data.managers.length > 0) {
            data.managers.forEach(manager => {
              this.managers[manager.id] = {
                id: manager.id,
                name: manager.name || manager.displayName || manager.email || 'Unknown',
                displayName: manager.displayName,
                email: manager.email
              };
              totalManagers++;
            });
            
            console.log(`Loaded ${data.managers.length} managers (total: ${totalManagers})`);
            
            if (data.managers.length < 500) {
              hasMore = false;
            } else {
              offset += 500;
            }
          } else {
            hasMore = false;
          }
        } catch (error) {
          console.error(`Error loading managers at offset ${offset}:`, error);
          hasMore = false;
        }
      }
      
      console.log(`✅ Total managers loaded: ${totalManagers}`);
      
    } catch (error) {
      console.error('Failed to load managers:', error);
    }
  }

  findTeamByName(name) {
    if (!name || name === '미배정') return '없음';
    
    const fullName = String(name).trim();
    
    for (const [team, members] of Object.entries(this.teamMembers)) {
      if (members.includes(fullName)) {
        return team;
      }
    }
    
    for (const [team, members] of Object.entries(this.teamMembers)) {
      for (const member of members) {
        if (fullName.includes(member) || member.includes(fullName)) {
          return team;
        }
      }
    }
    
    return '기타';
  }

  // 빠른 초기 동기화 (최근 500개만)
  async quickSync() {
    try {
      console.log('=== Quick sync starting ===');
      this.apiCallCount = 0;
      
      // 매니저 정보 로드
      if (Object.keys(this.managers).length === 0) {
        await this.loadManagers();
      }
      
      // 최근 500개만 빠르게 가져오기
      const data = await this.makeRequest('/user-chats?state=opened&limit=500&sortOrder=desc');
      const userChats = data.userChats || [];
      
      console.log(`Quick sync: Found ${userChats.length} recent chats`);
      
      // 미답변 상담만 빠르게 처리
      const unansweredChats = [];
      const batchSize = 10;
      
      for (let i = 0; i < userChats.length; i += batchSize) {
        const batch = userChats.slice(i, i + batchSize);
        
        await Promise.all(batch.map(async (chat) => {
          try {
            const messagesData = await this.makeRequest(`/user-chats/${chat.id}/messages?sortOrder=desc&limit=3`);
            const messages = messagesData.messages || [];
            
            const lastCustomerMessage = messages.find(m => m.personType === 'user');
            const lastManagerMessage = messages.find(m => m.personType === 'manager');
            
            if (lastCustomerMessage && (!lastManagerMessage || lastCustomerMessage.createdAt > lastManagerMessage.createdAt)) {
              // 담당자 정보
              let counselorName = '미배정';
              let teamName = '없음';
              
              if (chat.assigneeId && this.managers[chat.assigneeId]) {
                const assignee = this.managers[chat.assigneeId];
                counselorName = assignee.name || assignee.displayName || '미배정';
                teamName = this.findTeamByName(counselorName);
              }
              
              // 고객 정보
              let customerName = chat.name || chat.user?.name || chat.user?.phoneNumber || '익명';
              
              const consultationData = {
                id: String(chat.id),
                customerName: String(customerName),
                customerMessage: String(lastCustomerMessage.plainText || lastCustomerMessage.message || ''),
                team: String(teamName),
                counselor: String(counselorName),
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
            console.error(`Error in quick sync for chat ${chat.id}:`, error.message);
          }
        }));
        
        // 배치 간 짧은 딜레이
        if (i + batchSize < userChats.length) {
          await this.delay(500);
        }
      }
      
      console.log(`=== Quick sync complete: ${unansweredChats.length} unanswered chats (${this.apiCallCount} API calls) ===`);
      
      // 정렬
      unansweredChats.sort((a, b) => {
        const waitTimeDiff = parseInt(b.waitTime || 0) - parseInt(a.waitTime || 0);
        if (waitTimeDiff !== 0) return waitTimeDiff;
        
        const nameA = a.customerName || '익명';
        const nameB = b.customerName || '익명';
        return nameB.localeCompare(nameA, 'ko');
      });
      
      // 대시보드 업데이트
      this.io.to('dashboard').emit('dashboard:update', unansweredChats);
      
      // 첫 동기화 시간 설정
      this.lastSyncTime = Date.now();
      
    } catch (error) {
      console.error('Quick sync error:', error);
    }
  }

  // 전체 동기화
  async syncOpenChats() {
    try {
      console.log('=== Starting sync ===');
      this.apiCallCount = 0;
      
      // 최소 동기화 간격 설정 (60초)
      const now = Date.now();
      if (now -
